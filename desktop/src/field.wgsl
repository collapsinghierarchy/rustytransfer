struct Uniforms {
    size_time: vec4<f32>,
    pointer: vec4<f32>,
    lens: vec4<f32>,
}
@group(0) @binding(0) var<uniform> u: Uniforms;

// Keep these equations aligned with field.rs (CPU hit testing and node labels).
fn warp_pointer(p: vec2<f32>) -> vec2<f32> {
    if u.pointer.z == 0.0 { return p; }
    let delta = p - u.pointer.xy;
    let d = max(length(delta), 1.0);
    if d >= 102.0 { return p; }
    let t = 1.0 - d / 102.0;
    let vortex = 30.0 * t * t * (1.0 - 0.16 * t);
    let radial = -16.0 * t * t;
    let shear = 10.0 * sin((delta.x - delta.y) * 0.052) * t;
    return p + delta / d * radial + vec2(-delta.y, delta.x) / d * vortex
        + vec2(delta.y, -delta.x) / 102.0 * shear;
}
fn warp_lens(p: vec2<f32>) -> vec2<f32> {
    let core = u.lens.z;
    let radius = u.lens.w;
    if radius == 0.0 { return p; }
    let delta = p - u.lens.xy;
    let d = length(delta);
    if d >= radius { return p; }
    var unit = vec2(1.0, 0.0);
    if d >= 0.001 { unit = delta / d; }
    let scale = core / 500.0;
    var radial: f32;
    var tangent: f32;
    if d < core {
        let depth = 1.0 - d / core;
        radial = core + 28.0 * scale - d + 152.0 * scale * pow(depth, 1.32);
        tangent = 192.0 * scale * (0.38 + 0.62 * depth);
    } else {
        let band = (radius - d) / (radius - core);
        radial = 178.0 * scale * pow(band, 1.55);
        tangent = 228.0 * scale * pow(sin(1.57079632679 * band), 1.18);
    }
    let fall = 1.0 - d / radius;
    let shear = 27.0 * scale * sin((delta.x - delta.y) * 0.022) * fall;
    let breathe = 8.0 * scale * sin(d * 0.021 + atan2(delta.y, delta.x) * 1.7) * fall;
    return p + unit * (radial + breathe) + vec2(-unit.y, unit.x) * tangent
        + vec2(delta.y, -delta.x) / radius * shear;
}
struct Output {
    @builtin(position) position: vec4<f32>,
    @location(0) local: vec2<f32>,
    @location(1) color: vec4<f32>,
    @location(2) radius: f32,
}
@vertex fn vs_main(@builtin(vertex_index) vertex: u32,
    @location(0) position: vec2<f32>, @location(1) radius: f32,
    @location(2) kind: f32, @location(3) color: vec4<f32>, @location(4) packet: vec4<f32>) -> Output {
    let corners = array<vec2<f32>,6>(vec2(-1.0,-1.0),vec2(1.0,-1.0),vec2(-1.0,1.0),
                                    vec2(-1.0,1.0),vec2(1.0,-1.0),vec2(1.0,1.0));
    var r = radius;
    var c = color;
    if kind == 1.0 && packet.z == 1.0 && u.size_time.w == 0.0 {
        let head = floor(u.size_time.z * 14.2857) % max(packet.y, 1.0);
        if abs(packet.x - head) < 1.0 { r = 3.1; c = vec4(0.92,0.97,1.0,1.0); }
    }
    let local = corners[vertex] * (r + 1.0);
    let p = warp_lens(warp_pointer(position)) + local;
    var output: Output;
    output.position = vec4(p.x / u.size_time.x * 2.0 - 1.0, 1.0 - p.y / u.size_time.y * 2.0, 0.0, 1.0);
    output.local = local;
    output.color = c;
    output.radius = r;
    return output;
}
fn linear(c: vec3<f32>) -> vec3<f32> {
    return select(pow((c + vec3(0.055)) / 1.055, vec3(2.4)), c / 12.92, c <= vec3(0.04045));
}
@fragment fn fs_main(input: Output) -> @location(0) vec4<f32> {
    let edge = max(fwidth(length(input.local)), 0.55);
    let alpha = 1.0 - smoothstep(input.radius - edge * 0.5, input.radius + edge * 0.5, length(input.local));
    return vec4(linear(input.color.rgb), input.color.a * alpha);
}
