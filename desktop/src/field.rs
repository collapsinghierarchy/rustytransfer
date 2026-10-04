//! Shared geometry: GPU rendering and CPU hit testing use the same deformation.
use crate::model::{NodeKind, RouteKind, RouteState, RouteTopology, Scene};
use iced::{Color, Point, Size};
use std::{collections::BTreeMap, sync::Arc};

pub const BG: Color = Color::from_rgb8(8, 16, 27);
pub const BLUE: Color = Color::from_rgb8(47, 128, 255);
pub const DIRECT: Color = Color::from_rgb8(121, 199, 255);
pub const ORANGE: Color = Color::from_rgb8(255, 138, 42);
pub const RELAY: Color = Color::from_rgb8(228, 174, 72);
pub const GREEN: Color = Color::from_rgb8(84, 199, 155);
pub const FAILED: Color = Color::from_rgb8(229, 110, 115);

#[repr(C)]
#[derive(Debug, Clone, Copy, bytemuck::Pod, bytemuck::Zeroable)]
pub struct Dot {
    pub position: [f32; 2],
    pub radius: f32,
    pub kind: f32,
    pub color: [f32; 4],
    pub packet: [f32; 4],
}

#[derive(Debug, Clone, Copy)]
pub struct Lens {
    pub center: Point,
    pub core: f32,
    pub radius: f32,
}
impl Lens {
    pub fn new(center: Point, size: Size) -> Self {
        let core = (size.height.min(size.width) * 0.36).clamp(150.0, 260.0);
        Self {
            center,
            core,
            radius: core * 1.32,
        }
    }
    pub fn warp(self, p: Point) -> Point {
        let dx = p.x - self.center.x;
        let dy = p.y - self.center.y;
        let d = dx.hypot(dy);
        if d >= self.radius {
            return p;
        }
        // The exact center needs a direction too; never leave geometry inside text.
        let (ux, uy) = if d < 0.001 {
            (1.0, 0.0)
        } else {
            (dx / d, dy / d)
        };
        let scale = self.core / 500.0;
        let (radial, tangent) = if d < self.core {
            let depth = 1.0 - d / self.core;
            (
                (self.core + 28.0 * scale - d) + 152.0 * scale * depth.powf(1.32),
                192.0 * scale * (0.38 + 0.62 * depth),
            )
        } else {
            let band = (self.radius - d) / (self.radius - self.core);
            (
                178.0 * scale * band.powf(1.55),
                228.0 * scale * (std::f32::consts::FRAC_PI_2 * band).sin().powf(1.18),
            )
        };
        let fall = 1.0 - d / self.radius;
        let shear = 27.0 * scale * ((dx - dy) * 0.022).sin() * fall;
        let breathe = 8.0 * scale * (d * 0.021 + dy.atan2(dx) * 1.7).sin() * fall;
        Point::new(
            p.x + ux * (radial + breathe) - uy * tangent + dy / self.radius * shear,
            p.y + uy * (radial + breathe) + ux * tangent - dx / self.radius * shear,
        )
    }
}

pub fn pointer_warp(p: Point, pointer: Option<Point>) -> Point {
    let Some(pointer) = pointer else {
        return p;
    };
    let dx = p.x - pointer.x;
    let dy = p.y - pointer.y;
    let d = dx.hypot(dy).max(1.0);
    if d >= 102.0 {
        return p;
    }
    let t = 1.0 - d / 102.0;
    let vortex = 30.0 * t * t * (1.0 - 0.16 * t);
    let radial = -16.0 * t * t;
    let shear = 10.0 * ((dx - dy) * 0.052).sin() * t;
    Point::new(
        p.x + dx / d * radial - dy / d * vortex + dy / 102.0 * shear,
        p.y + dy / d * radial + dx / d * vortex - dx / 102.0 * shear,
    )
}
pub fn warp(p: Point, pointer: Option<Point>, lens: Option<Lens>) -> Point {
    let p = pointer_warp(p, pointer);
    lens.map_or(p, |lens| lens.warp(p))
}

#[derive(Debug, Clone)]
pub struct RouteGeometry {
    pub id: String,
    pub points: Vec<Point>,
    pub color: Color,
}
#[derive(Debug, Clone)]
pub struct Field {
    pub dots: Arc<Vec<Dot>>,
    pub routes: Vec<RouteGeometry>,
    pub nodes: BTreeMap<String, Point>,
    pub revision: u64,
    pub size: Size,
}
impl Default for Field {
    fn default() -> Self {
        Self {
            dots: Arc::new(Vec::new()),
            routes: Vec::new(),
            nodes: BTreeMap::new(),
            revision: 0,
            size: Size::ZERO,
        }
    }
}
pub fn node_color(kind: NodeKind) -> Color {
    match kind {
        NodeKind::LocalSource | NodeKind::Signaling => BLUE,
        NodeKind::Relay => RELAY,
        NodeKind::Router | NodeKind::Service => GREEN,
        NodeKind::Peer => ORANGE,
    }
}
pub fn route_color(kind: RouteKind, topology: RouteTopology, state: RouteState) -> Color {
    if state == RouteState::Failed {
        FAILED
    } else if kind == RouteKind::Signaling {
        BLUE
    } else if topology == RouteTopology::Relay {
        RELAY
    } else {
        DIRECT
    }
}
fn rgba(color: Color) -> [f32; 4] {
    [color.r, color.g, color.b, color.a]
}

impl Field {
    pub fn rebuild(&mut self, scene: &Scene, size: Size, overrides: &BTreeMap<String, Point>) {
        self.size = size;
        self.revision += 1;
        self.nodes.clear();
        if let Some(source) = &scene.source {
            self.nodes.insert(
                source.id.clone(),
                Point::new(size.width * 0.12, size.height * 0.22),
            );
        }
        let mut peer = 0;
        for node in scene.nodes.values() {
            let p = match node.kind {
                NodeKind::Signaling => Point::new(size.width * 0.49, size.height * 0.28),
                NodeKind::Relay => Point::new(size.width * 0.57, size.height * 0.78),
                NodeKind::Router => Point::new(size.width * 0.30, size.height * 0.42),
                NodeKind::Service => Point::new(size.width * 0.23, size.height * 0.74),
                _ => {
                    peer += 1;
                    Point::new(
                        size.width * 0.86,
                        size.height * (0.45 + (peer - 1) as f32 * 0.17),
                    )
                }
            };
            self.nodes.insert(
                node.id.clone(),
                overrides.get(&node.id).copied().unwrap_or(p),
            );
        }
        self.routes.clear();
        for route in scene.routes.values() {
            let (Some(&from), Some(&to)) = (self.nodes.get(&route.from), self.nodes.get(&route.to))
            else {
                continue;
            };
            let mut anchors = vec![from];
            if route.via.is_empty() && route.kind == RouteKind::Payload {
                anchors.push(Point::new(size.width * 0.42, size.height * 0.72));
            }
            anchors.extend(
                route
                    .via
                    .iter()
                    .filter_map(|id| self.nodes.get(id))
                    .copied(),
            );
            anchors.push(to);
            let mut points = Vec::new();
            for segment in anchors.windows(2) {
                grid_line(segment[0], segment[1], &mut points);
            }
            self.routes.push(RouteGeometry {
                id: route.id.clone(),
                points,
                color: route_color(route.kind, route.topology, route.state),
            });
        }
        // Cache the route trench field. Pointer/lens motion only changes GPU uniforms.
        let cols = (size.width / 7.0).ceil() as usize + 1;
        let rows = (size.height / 7.0).ceil() as usize + 1;
        let mut offsets = vec![(0.0_f32, 0.0_f32, 0.0_f32); cols * rows];
        for route in &self.routes {
            for (i, p) in route.points.iter().enumerate() {
                let before = route.points[i.saturating_sub(1)];
                let after = route.points[(i + 1).min(route.points.len() - 1)];
                let tx = after.x - before.x;
                let ty = after.y - before.y;
                let length = tx.hypot(ty).max(1.0);
                let (nx, ny) = (-ty / length, tx / length);
                let (col, row) = ((p.x / 7.0).round() as i32, (p.y / 7.0).round() as i32);
                for dr in -8_i32..=8 {
                    for dc in -8_i32..=8 {
                        let (c, r) = (col + dc, row + dr);
                        if c < 0 || r < 0 || c >= cols as i32 || r >= rows as i32 {
                            continue;
                        }
                        let distance = (dc as f32).hypot(dr as f32);
                        if distance >= 8.0 {
                            continue;
                        }
                        let side = dc as f32 * nx + dr as f32 * ny;
                        let fall = (1.0 - distance / 8.0).powf(2.15);
                        let trench = -side.signum() * side.abs().min(2.2) * 2.4 * fall;
                        let wake = (i as f32 * 0.31 + distance * 0.62).sin() * 2.3 * fall;
                        let entry = &mut offsets[r as usize * cols + c as usize];
                        entry.0 += nx * (trench + wake) + tx / length * 0.5 * wake;
                        entry.1 += ny * (trench + wake) + ty / length * 0.5 * wake;
                        entry.2 += fall * 0.42;
                    }
                }
            }
        }
        let mut dots = Vec::with_capacity(cols * rows + 1000);
        for row in 0..rows {
            for col in 0..cols {
                let (ox, oy, energy) = offsets[row * cols + col];
                let p = self.node_warp(Point::new(col as f32 * 7.0, row as f32 * 7.0));
                let light = energy.min(1.0);
                dots.push(Dot {
                    position: [p.x + ox, p.y + oy],
                    radius: 0.68 + light * 0.16,
                    kind: 0.0,
                    color: [
                        0.125 + light * 0.06,
                        0.188 + light * 0.075,
                        0.267 + light * 0.094,
                        1.0,
                    ],
                    packet: [0.0; 4],
                });
            }
        }
        for geometry in &mut self.routes {
            let Some(route) = scene.routes.get(&geometry.id) else {
                continue;
            };
            let active = route.kind == RouteKind::Payload
                && route.state == RouteState::Connected
                && route
                    .transfer_id
                    .as_ref()
                    .and_then(|id| scene.transfers.get(id))
                    .is_some_and(|t| t.state == crate::model::TransferState::Transferring);
            let count = geometry.points.len();
            // Retain exactly the positions rendered, including the static trench warp.
            for (i, p) in geometry.points.iter_mut().enumerate() {
                let c = (p.x / 7.0).round().clamp(0.0, (cols - 1) as f32) as usize;
                let r = (p.y / 7.0).round().clamp(0.0, (rows - 1) as f32) as usize;
                let (ox, oy, _) = offsets[r * cols + c];
                p.x += ox;
                p.y += oy;
                dots.push(Dot {
                    position: [p.x, p.y],
                    radius: 1.8,
                    kind: 1.0,
                    color: rgba(geometry.color),
                    packet: [i as f32, count as f32, if active { 1.0 } else { 0.0 }, 0.0],
                });
            }
        }
        self.dots = Arc::new(dots);
    }
    fn node_warp(&self, p: Point) -> Point {
        let mut result = p;
        for (i, node) in self.nodes.values().enumerate() {
            let dx = p.x - node.x;
            let dy = p.y - node.y;
            let d = dx.hypot(dy).max(1.0);
            let radius = if i == 0 { 78.0 } else { 48.0 };
            if d >= radius {
                continue;
            }
            let t = 1.0 - d / radius;
            let radial = -4.1 * t * t;
            let twist = 6.1 * (std::f32::consts::PI * t).sin() * t;
            result.x += dx / d * radial - dy / d * twist;
            result.y += dy / d * radial + dx / d * twist;
        }
        result
    }
    pub fn hit(
        &self,
        position: Point,
        pointer: Option<Point>,
        lens: Option<Lens>,
    ) -> Option<Selection> {
        for (id, point) in &self.nodes {
            if warp(*point, pointer, lens).distance(position) < 27.0 {
                return Some(Selection::Node(id.clone()));
            }
        }
        self.routes
            .iter()
            .find(|r| {
                r.points
                    .iter()
                    .any(|p| warp(*p, pointer, lens).distance(position) < 18.0)
            })
            .map(|r| Selection::Route(r.id.clone()))
    }
}
#[derive(Debug, Clone, PartialEq)]
pub enum Selection {
    Node(String),
    Route(String),
}

fn grid_line(from: Point, to: Point, points: &mut Vec<Point>) {
    let (mut x, mut y) = ((from.x / 7.0).round() as i32, (from.y / 7.0).round() as i32);
    let (tx, ty) = ((to.x / 7.0).round() as i32, (to.y / 7.0).round() as i32);
    let (dx, dy) = ((tx - x).abs(), -(ty - y).abs());
    let (sx, sy) = (if x < tx { 1 } else { -1 }, if y < ty { 1 } else { -1 });
    let mut err = dx + dy;
    loop {
        let p = Point::new(x as f32 * 7.0, y as f32 * 7.0);
        if points.last() != Some(&p) {
            points.push(p);
        }
        if x == tx && y == ty {
            break;
        }
        let e = 2 * err;
        if e >= dy {
            err += dy;
            x += sx;
        }
        if e <= dx {
            err += dx;
            y += sy;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn lens_evicts_topology_from_information_core_including_exact_center() {
        let lens = Lens::new(Point::new(600.0, 350.0), Size::new(1200.0, 700.0));
        for x in -240..=240 {
            for y in (-240..=240).step_by(8) {
                let p = Point::new(600.0 + x as f32, 350.0 + y as f32);
                let moved = lens.warp(p);
                assert!(moved.x.is_finite() && moved.y.is_finite());
                if p.distance(lens.center) < lens.core {
                    assert!(moved.distance(lens.center) > lens.core);
                }
            }
        }
    }
    #[test]
    fn deformation_has_bounded_support() {
        let p = Point::new(1000.0, 1000.0);
        assert_eq!(pointer_warp(p, Some(Point::ORIGIN)), p);
        assert_eq!(Lens::new(Point::ORIGIN, Size::new(800.0, 600.0)).warp(p), p);
    }
    #[test]
    fn route_hit_testing_follows_warped_geometry() {
        let mut scene = Scene::default();
        for event in crate::model::parse_ndjson(include_str!("../fixtures/direct.ndjson")).unwrap()
        {
            scene.apply(event).unwrap();
        }
        let mut field = Field::default();
        field.rebuild(&scene, Size::new(1200.0, 700.0), &BTreeMap::new());
        let lens = Some(Lens::new(Point::new(600.0, 350.0), field.size));
        let geometry = field.routes.iter().find(|r| r.id == "payload-t1").unwrap();
        let p = warp(geometry.points[geometry.points.len() / 2], None, lens);
        assert_eq!(
            field.hit(p, None, lens),
            Some(Selection::Route("payload-t1".into()))
        );
    }
}
