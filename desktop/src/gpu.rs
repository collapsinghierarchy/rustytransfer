//! Instanced dots: geometry uploads only on scene/size changes, motion in WGSL.
use crate::field::{Dot, Field, Lens};
use iced::{
    Rectangle, mouse, wgpu,
    widget::shader::{self, Viewport},
};
use std::sync::Arc;

#[repr(C)]
#[derive(Debug, Clone, Copy, bytemuck::Pod, bytemuck::Zeroable)]
struct Uniforms {
    size_time: [f32; 4],
    pointer: [f32; 4],
    lens: [f32; 4],
}
pub struct View<'a> {
    pub field: &'a Field,
    pub pointer: Option<iced::Point>,
    pub lens: Option<Lens>,
    pub time: f32,
    pub reduced_motion: bool,
}
impl<Message> shader::Program<Message> for View<'_> {
    type State = ();
    type Primitive = Primitive;
    fn draw(&self, _: &(), _: mouse::Cursor, bounds: Rectangle) -> Primitive {
        let pointer = self.pointer.filter(|_| !self.reduced_motion);
        Primitive {
            dots: Arc::clone(&self.field.dots),
            revision: self.field.revision,
            bounds,
            uniforms: Uniforms {
                size_time: [
                    bounds.width,
                    bounds.height,
                    self.time,
                    if self.reduced_motion { 1.0 } else { 0.0 },
                ],
                pointer: pointer.map_or([0.0; 4], |p| [p.x, p.y, 1.0, 0.0]),
                lens: self
                    .lens
                    .map_or([0.0; 4], |l| [l.center.x, l.center.y, l.core, l.radius]),
            },
        }
    }
}
#[derive(Debug)]
pub struct Primitive {
    dots: Arc<Vec<Dot>>,
    revision: u64,
    uniforms: Uniforms,
    bounds: Rectangle,
}
pub struct Pipeline {
    pipeline: wgpu::RenderPipeline,
    uniforms: wgpu::Buffer,
    bind_group: wgpu::BindGroup,
    instances: wgpu::Buffer,
    capacity: u64,
    revision: Option<u64>,
    viewport: [f32; 4],
}
impl shader::Pipeline for Pipeline {
    fn new(device: &wgpu::Device, _: &wgpu::Queue, format: wgpu::TextureFormat) -> Self {
        let uniforms = device.create_buffer(&wgpu::BufferDescriptor {
            label: Some("field uniforms"),
            size: std::mem::size_of::<Uniforms>() as u64,
            usage: wgpu::BufferUsages::UNIFORM | wgpu::BufferUsages::COPY_DST,
            mapped_at_creation: false,
        });
        let layout = device.create_bind_group_layout(&wgpu::BindGroupLayoutDescriptor {
            label: Some("field bind layout"),
            entries: &[wgpu::BindGroupLayoutEntry {
                binding: 0,
                visibility: wgpu::ShaderStages::VERTEX_FRAGMENT,
                ty: wgpu::BindingType::Buffer {
                    ty: wgpu::BufferBindingType::Uniform,
                    has_dynamic_offset: false,
                    min_binding_size: None,
                },
                count: None,
            }],
        });
        let bind_group = device.create_bind_group(&wgpu::BindGroupDescriptor {
            label: Some("field bind group"),
            layout: &layout,
            entries: &[wgpu::BindGroupEntry {
                binding: 0,
                resource: uniforms.as_entire_binding(),
            }],
        });
        let shader = device.create_shader_module(wgpu::ShaderModuleDescriptor {
            label: Some("coherent field shader"),
            source: wgpu::ShaderSource::Wgsl(include_str!("field.wgsl").into()),
        });
        let pipeline_layout = device.create_pipeline_layout(&wgpu::PipelineLayoutDescriptor {
            label: Some("field pipeline layout"),
            bind_group_layouts: &[&layout],
            push_constant_ranges: &[],
        });
        let pipeline = device.create_render_pipeline(&wgpu::RenderPipelineDescriptor {
            label: Some("field dots"), layout: Some(&pipeline_layout),
            vertex: wgpu::VertexState {
                module: &shader, entry_point: Some("vs_main"), compilation_options: Default::default(),
                buffers: &[wgpu::VertexBufferLayout {
                    array_stride: std::mem::size_of::<Dot>() as u64, step_mode: wgpu::VertexStepMode::Instance,
                    attributes: &wgpu::vertex_attr_array![0 => Float32x2, 1 => Float32, 2 => Float32, 3 => Float32x4, 4 => Float32x4],
                }],
            },
            fragment: Some(wgpu::FragmentState {
                module: &shader, entry_point: Some("fs_main"), compilation_options: Default::default(),
                targets: &[Some(wgpu::ColorTargetState { format,
                    blend: Some(wgpu::BlendState::ALPHA_BLENDING), write_mask: wgpu::ColorWrites::ALL })],
            }),
            primitive: Default::default(), depth_stencil: None, multisample: Default::default(),
            multiview: None, cache: None,
        });
        let instances = instance_buffer(device, 48);
        Self {
            pipeline,
            uniforms,
            bind_group,
            instances,
            capacity: 48,
            revision: None,
            viewport: [0.0; 4],
        }
    }
}
fn instance_buffer(device: &wgpu::Device, size: u64) -> wgpu::Buffer {
    device.create_buffer(&wgpu::BufferDescriptor {
        label: Some("cached field instances"),
        size,
        usage: wgpu::BufferUsages::VERTEX | wgpu::BufferUsages::COPY_DST,
        mapped_at_creation: false,
    })
}
impl shader::Primitive for Primitive {
    type Pipeline = Pipeline;
    fn prepare(
        &self,
        pipeline: &mut Pipeline,
        device: &wgpu::Device,
        queue: &wgpu::Queue,
        _: &Rectangle,
        viewport: &Viewport,
    ) {
        if pipeline.revision != Some(self.revision) {
            let bytes = bytemuck::cast_slice(self.dots.as_slice());
            if bytes.len() as u64 > pipeline.capacity {
                pipeline.capacity = (bytes.len() as u64).next_power_of_two();
                pipeline.instances = instance_buffer(device, pipeline.capacity);
            }
            if !bytes.is_empty() {
                queue.write_buffer(&pipeline.instances, 0, bytes);
            }
            pipeline.revision = Some(self.revision);
        }
        queue.write_buffer(&pipeline.uniforms, 0, bytemuck::bytes_of(&self.uniforms));
        let scale = viewport.scale_factor();
        pipeline.viewport = [
            self.bounds.x * scale,
            self.bounds.y * scale,
            self.bounds.width * scale,
            self.bounds.height * scale,
        ];
    }
    fn render(
        &self,
        pipeline: &Pipeline,
        encoder: &mut wgpu::CommandEncoder,
        target: &wgpu::TextureView,
        clip: &Rectangle<u32>,
    ) {
        if self.dots.is_empty() || clip.width == 0 || clip.height == 0 {
            return;
        }
        let mut pass = encoder.begin_render_pass(&wgpu::RenderPassDescriptor {
            label: Some("coherent field pass"),
            color_attachments: &[Some(wgpu::RenderPassColorAttachment {
                view: target,
                depth_slice: None,
                resolve_target: None,
                ops: wgpu::Operations {
                    load: wgpu::LoadOp::Load,
                    store: wgpu::StoreOp::Store,
                },
            })],
            depth_stencil_attachment: None,
            timestamp_writes: None,
            occlusion_query_set: None,
        });
        let [x, y, w, h] = pipeline.viewport;
        pass.set_viewport(x, y, w, h, 0.0, 1.0);
        pass.set_scissor_rect(clip.x, clip.y, clip.width, clip.height);
        pass.set_pipeline(&pipeline.pipeline);
        pass.set_bind_group(0, &pipeline.bind_group, &[]);
        pass.set_vertex_buffer(0, pipeline.instances.slice(..));
        pass.draw(0..6, 0..self.dots.len() as u32);
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn wgsl_parses_and_validates() {
        let module = naga::front::wgsl::parse_str(include_str!("field.wgsl")).expect("valid WGSL");
        naga::valid::Validator::new(
            naga::valid::ValidationFlags::all(),
            naga::valid::Capabilities::empty(),
        )
        .validate(&module)
        .expect("valid shader interfaces");
    }
}
