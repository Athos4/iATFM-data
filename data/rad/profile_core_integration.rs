/// Native PTR bounds passed by the RAD condition/calendar evaluator. They
/// affect the computed profile and never change the filed flight plan.
#[derive(Clone, Debug)]
pub struct NativeProfileConstraint {
    pub point: String,
    pub min_ft: Option<f64>,
    pub max_ft: Option<f64>,
    /// When present, also verify every generated profile vertex through this
    /// endpoint. Bounds on fixes alone must not hide a peak between them.
    pub through: Option<String>,
    pub distance_span: Option<(f64,f64)>,
}

#[allow(clippy::too_many_arguments)]
pub fn build_native_constrained_profile(
    sampled_at: DateTime<Utc>, pilot: &Pilot, atcs: &[Controller],
    route_navdata: &RouteNavdata, navdata: &Navdata,
    airport_winds: &BTreeMap<String, AirportWind>,
    performance: &AircraftPerformanceDatabase, weather: Option<&WeatherModel>,
    constraints: &[NativeProfileConstraint],
) -> Result<Vec<VerticalProfilePoint>,String> {
    let mut merged=BTreeMap::<String,AltitudeConstraint>::new();
    for cap in constraints {
        if (cap.point.is_empty() && cap.distance_span.is_none()) || (cap.min_ft.is_none() && cap.max_ft.is_none())
            || [cap.min_ft,cap.max_ft].into_iter().flatten().any(|v|!v.is_finite() || v<0.) {
            return Err("Invalid native profile constraint".into());
        }
        if let Some((a,b))=cap.distance_span {
            if !a.is_finite() || !b.is_finite() || a<0. || a>b {return Err("Invalid native profile interval".into());}
            continue;
        }
        let entry=merged.entry(cap.point.clone()).or_default();
        if let Some(v)=cap.min_ft {entry.min_ft=Some(entry.min_ft.map_or(v,|old|old.max(v)));}
        if let Some(v)=cap.max_ft {entry.max_ft=Some(entry.max_ft.map_or(v,|old|old.min(v)));}
        if entry.min_ft.zip(entry.max_ft).is_some_and(|(a,b)|a>b) {
            return Err(format!("Conflicting native profile bounds at {}",cap.point));
        }
    }
    let caps=merged.iter().map(|(point,constraint)|MatchedLoaConstraint {
        point:point.clone(),constraint:*constraint,
    }).collect::<Vec<_>>();
    let lookup=build_aircraft_performance_lookup(performance);
    let runways=build_active_runway_designators_by_airport(atcs);
    let trajectory=build_flight_plan_trajectory_native(&sampled_at,pilot,route_navdata,navdata,
        &runways,airport_winds,aircraft_performance_for_pilot(pilot,&lookup),weather,&[],&[],&caps,constraints)
        .ok_or("Cannot build the constrained native profile")?;
    let points=trajectory.route_point_profiles.into_iter().map(|p|VerticalProfilePoint {
        name:p.name,distance_nm:p.distance_offset_nm.filter(|v|v.is_finite()),
        altitude_ft:p.altitude_ft.filter(|v|v.is_finite()),minute_offset:p.minute_offset,position:p.point,
    }).collect::<Vec<_>>();
    // Procedure/performance reconciliation must not silently erase a bound.
    for (id,cap) in merged {
        let matches=points.iter().filter(|p|p.name==id).collect::<Vec<_>>();
        if matches.len()!=1 || matches[0].altitude_ft.is_none_or(|h|
            cap.min_ft.is_some_and(|v|h<v-0.01) || cap.max_ft.is_some_and(|v|h>v+0.01)) {
            return Err(format!("Native profile bound could not be satisfied at {id}"));
        }
    }
    for cap in constraints.iter().filter(|c|c.through.is_some()) {
        let end=cap.through.as_ref().unwrap();
        let starts=points.iter().enumerate().filter(|(_,p)|p.name==cap.point).map(|(i,_)|i).collect::<Vec<_>>();
        let ends=points.iter().enumerate().filter(|(_,p)|p.name==*end).map(|(i,_)|i).collect::<Vec<_>>();
        if starts.len()!=1 || ends.len()!=1 {return Err("Ambiguous native PTR extension endpoints".into());}
        let (a,b)=(starts[0].min(ends[0]),starts[0].max(ends[0]));
        if points[a..=b].iter().any(|p|p.altitude_ft.is_none_or(|h|
            cap.min_ft.is_some_and(|v|h<v-0.01) || cap.max_ft.is_some_and(|v|h>v+0.01))) {
            return Err(format!("Native PTR extension from {} to {end} could not be satisfied",cap.point));
        }
    }
    for cap in constraints.iter().filter(|c|c.distance_span.is_some()) {
        let (a,b)=cap.distance_span.unwrap();
        if points.last().and_then(|p|p.distance_nm).is_none_or(|d|d<b-1e-5) {return Err("Native interval extends beyond resolved trajectory".into());}
        let mut samples=points.iter().filter_map(|p|p.distance_nm.filter(|d|*d>=a && *d<=b).map(|_|p.altitude_ft)).collect::<Vec<_>>();
        for boundary in [a,b] {
            let height=points.windows(2).find_map(|p| {
                let (x,y,ha,hb)=(p[0].distance_nm?,p[1].distance_nm?,p[0].altitude_ft?,p[1].altitude_ft?);
                (x<=boundary && boundary<=y && x<y).then(||ha+(hb-ha)*(boundary-x)/(y-x))
            });
            samples.push(height);
        }
        if samples.iter().any(|h|h.is_none_or(|h|cap.min_ft.is_some_and(|v|h<v-0.01)
            || cap.max_ft.is_some_and(|v|h>v+0.01))) {return Err("Native airspace profile interval could not be satisfied".into());}
    }
    Ok(points)
}

fn native_profile_arc_point(from:Point,to:Point,fraction:f64)->Option<Point> {
    let vector=|p:Point| {let (lat,lon)=(p.lat.to_radians(),p.lon.to_radians());[lat.cos()*lon.cos(),lat.cos()*lon.sin(),lat.sin()]};
    let (a,b)=(vector(from),vector(to));let angle=a.iter().zip(b).map(|(x,y)|x*y).sum::<f64>().clamp(-1.,1.).acos();
    if !angle.is_finite() || std::f64::consts::PI-angle<1e-8 {return None;}
    if angle<1e-10 {return Some(from);}
    let (wa,wb)=(((1.-fraction)*angle).sin()/angle.sin(),(fraction*angle).sin()/angle.sin());
    let p=[wa*a[0]+wb*b[0],wa*a[1]+wb*b[1],wa*a[2]+wb*b[2]];
    Some(Point{lat:p[2].atan2(p[0].hypot(p[1])).to_degrees(),lon:p[1].atan2(p[0]).to_degrees()})
}

fn apply_native_profile_spans(points:&mut Vec<EnrichedRoutePoint>,constraints:&[NativeProfileConstraint],insert:bool)->bool {
    let spans=constraints.iter().filter(|c|c.distance_span.is_some()).collect::<Vec<_>>();
    if spans.is_empty() {return true;}
    if points.len()<2 || points.iter().any(|p|p.point.is_none()) {return false;}
    let distances=|points:&[EnrichedRoutePoint]| {
        let mut result=vec![0.];
        for pair in points.windows(2) {result.push(result.last().unwrap()+haversine_distance_nm(pair[0].point.unwrap(),pair[1].point.unwrap()));}
        result
    };
    let offsets=distances(points);
    if spans.iter().any(|c|c.distance_span.unwrap().1>*offsets.last().unwrap()+1e-5) {return false;}
    if insert {
        let mut boundaries=spans.iter().flat_map(|c|{let(a,b)=c.distance_span.unwrap();[a,b]}).collect::<Vec<_>>();
        boundaries.sort_by(f64::total_cmp);boundaries.dedup_by(|a,b|(*a-*b).abs()<1e-7);
        for index in (0..points.len()-1).rev() {
            let (a,b)=(offsets[index],offsets[index+1]);
            let (from,to)=(points[index].point.unwrap(),points[index+1].point.unwrap());
            for (ordinal,d) in boundaries.iter().enumerate().rev().filter(|(_,d)|**d>a+1e-7 && **d<b-1e-7) {
                let name=format!("NMP PTR BOUNDARY {ordinal}");
                if points.iter().any(|p|p.name==name) {return false;}
                let Some(position)=native_profile_arc_point(from,to,(*d-a)/(b-a)) else {return false;};
                points.insert(index+1,EnrichedRoutePoint {name,point:Some(position),altitude_constraint:None,
                    xfl_constraint:None,speed_constraint_kt:None,step_altitude_ft:None,
                    procedure_type:None,procedure_name:None,procedure_runway:None});
            }
        }
    }
    let offsets=distances(points);
    for (point,d) in points.iter_mut().zip(offsets) {
        for cap in &spans {
            let (a,b)=cap.distance_span.unwrap();
            if d<a-1e-5 || d>b+1e-5 {continue;}
            point.xfl_constraint=merge_altitude_constraints(point.xfl_constraint,
                Some(AltitudeConstraint{min_ft:cap.min_ft,max_ft:cap.max_ft}));
            if point.xfl_constraint.is_some_and(|c|c.min_ft.zip(c.max_ft).is_some_and(|(a,b)|a>b)) {return false;}
        }
    }
    true
}
