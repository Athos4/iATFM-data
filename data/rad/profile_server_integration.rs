// Minimum spherical distance to a minor great-circle arc, including endpoints.
// Antipodal endpoints do not define a unique arc and remain unresolved.
fn rad_native_arc_distance_nm(p:Point,a:Point,b:Point)->Option<f64> {
    fn vector(p:Point)->Option<[f64;3]> {
        if !p.lat.is_finite() || !p.lon.is_finite() || p.lat.abs()>90. || p.lon.abs()>180. {return None;}
        let (lat,lon)=(p.lat.to_radians(),p.lon.to_radians());Some([lat.cos()*lon.cos(),lat.cos()*lon.sin(),lat.sin()])
    }
    fn dot(a:[f64;3],b:[f64;3])->f64 {a.iter().zip(b).map(|(a,b)|a*b).sum()}
    fn angle(a:[f64;3],b:[f64;3])->f64 {dot(a,b).clamp(-1.,1.).acos()}
    let (p,a,b)=(vector(p)?,vector(a)?,vector(b)?);let arc=angle(a,b);
    let mut nearest=angle(p,a).min(angle(p,b));
    if arc<1e-10 {return Some(nearest*6371./1.852);}
    if std::f64::consts::PI-arc<1e-8 {return None;}
    let n=[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]];
    let norm=dot(n,n);let scale=dot(p,n)/norm;
    let q=[p[0]-scale*n[0],p[1]-scale*n[1],p[2]-scale*n[2]];let norm=dot(q,q).sqrt();
    if norm>1e-12 {
        for sign in [-1.,1.] {
            let q=[q[0]/norm*sign,q[1]/norm*sign,q[2]/norm*sign];
            if angle(a,q)+angle(q,b)<=arc+1e-8 {nearest=nearest.min(angle(p,q));}
        }
    }
    Some(nearest*6371./1.852)
}

impl AppState {
    fn native_profile_takeoff(&self,pilot:&Pilot,now:DateTime<Utc>)->DateTime<Utc> {
        let plan=pilot.flight_plan.as_ref();
        let date=plan.and_then(|p|p.date_of_flight.as_deref()).and_then(|s|
            chrono::NaiveDate::parse_from_str(s,"%Y%m%d").ok()
                .or_else(||chrono::NaiveDate::parse_from_str(s,"%y%m%d").ok())).unwrap_or(now.date_naive());
        let eobt=date.and_hms_opt(0,0,0).unwrap().and_utc()
            +ChronoDuration::minutes(i64::from(pilot_cdm_eobt(pilot).unwrap_or(0)));
        eobt.max(now)+ChronoDuration::minutes(i64::from(self.casa_parameters.taxi_minutes(plan.and_then(|p|p.departure_id.as_deref()))))
    }

    fn native_profile_constraints(&self,pilot:&Pilot,points:&[iatfm_core::dashboard::VerticalProfilePoint],
        reference:&ReferenceData,takeoff:DateTime<Utc>)
        ->(Vec<iatfm_core::dashboard::NativeProfileConstraint>,RadValue) {
        let mut metadata=rad_json!({"selectedRules":[],"pendingRules":[],"rawProfileTimeReference":true});
        let Ok(catalogue)=self.rad_catalogue() else {return (vec![],metadata);};
        let areas=SpecialAreaCache::default();
        let support=self.rad_support().unwrap_or_else(|_|Arc::new(rad_json!({})));
        let mut ctx=RadContext {
            support_offsets:rad_support_offsets(&support,takeoff),support,rule_key:String::new(),rule_id:String::new(),
            pilot,points,reference,areas:&areas,at:takeoff,takeoff,groups:BTreeMap::new(),aliases:BTreeMap::new(),
            geometry:BTreeMap::new(),visits:BTreeMap::new(),matches:BTreeMap::new(),area_lookup:BTreeMap::new(),
            candidate_lookup:BTreeMap::new(),sector_lookup:reference.sectors.iter().enumerate()
                .filter_map(|(i,s)|Some((s.id.clone()?,i))).collect(),
            procedure_ids:reference.navdata.procedures.values().flatten().map(|p|p.name.clone()).collect(),
            unresolved:BTreeSet::new(),spans:BTreeMap::new(),
        };
        let mut constraints=vec![];let mut applied=vec![];let mut pending=vec![];
        let arrival=points.last().and_then(|p|p.minute_offset).map(|m|takeoff+ChronoDuration::minutes(i64::from(m)));
        for rule in &catalogue.rules {
            let Some(caps)=rule["profileAdjustments"].as_array() else {continue;};
            if rad_str(rule,"effect")!="profileAdjustment" || matches!(rad_str(rule,"change"),"SUS"|"DEL")
                || !rad_database_state_overlaps(rule,takeoff,arrival) {continue;}
            ctx.rule_id=rad_str(rule,"id").into();ctx.rule_key=rad_str(rule,"key").into();
            let scope=ctx.eval(&rule["scope"]);
            if scope==RadTruth::No {continue;}
            let applies=scope.and(ctx.database_schedule(rule));
            if applies==RadTruth::No {continue;}
            if applies==RadTruth::Unknown || rad_native_rule_pending(rule) || caps.len()!=1 {
                pending.push(rule["id"].clone());continue;
            }
            let cap=&caps[0];
            if cap.get("forbiddenAirspaceBand").is_some() || cap.get("mandatoryAirspaceBand").is_some() || cap.get("routeLocation").is_some() || cap.get("profileChoices").is_some() {
                let result=ctx.database_profile_adjustment(cap);
                match result {
                    Some(caps)=>{constraints.extend(caps);applied.push(rule["id"].clone());},
                    None=>pending.push(rule["id"].clone()),
                }
                continue;
            }
            match ctx.database_point_profile_constraints(cap) {
                Some(caps)=>constraints.extend(caps),
                None=>{pending.push(rule["id"].clone());continue;},
            }
            applied.push(rule["id"].clone());
        }
        metadata["selectedRules"]=applied.into();metadata["pendingRules"]=pending.into();
        (constraints,metadata)
    }
}

impl RadContext<'_> {
    fn database_profile_adjustment(&mut self,cap:&RadValue)->Option<Vec<iatfm_core::dashboard::NativeProfileConstraint>> {
        if let Some(choices)=cap["profileChoices"].as_array() {
            let mandatory=match rad_str(cap,"mode") {"MANDATORY"=>true,"FORBIDDEN"=>false,_=>return None};
            let mut result=vec![];let mut unknown=false;let mut selected=false;
            for choice in choices {
                let scope=self.eval(&choice["scope"]);
                if scope==RadTruth::No {continue;}
                let applies=scope.and(self.database_schedule(choice));
                match applies {
                    RadTruth::No=>continue,
                    RadTruth::Unknown=>{unknown=true;continue;},
                    RadTruth::Yes=>{},
                }
                // Groups emitted by the compiler contain only leaf choices.
                if choice["adjustment"].get("profileChoices").is_some() {return None;}
                match self.database_profile_adjustment(&choice["adjustment"]) {
                    Some(caps)=>{selected=true;if mandatory {return Some(caps);}result.extend(caps);},
                    None=>unknown=true,
                }
            }
            return if unknown || (mandatory && !selected) {None}else{Some(result)};
        }
        if cap.get("routeLocation").is_some() {self.database_route_profile_constraints(cap)}
        else if cap.get("forbiddenAirspaceBand").is_some() || cap.get("mandatoryAirspaceBand").is_some() {self.database_airspace_profile_constraints(cap)}
        else {self.database_point_profile_constraints(cap)}
    }

    fn database_point_profile_constraints(&mut self,cap:&RadValue)->Option<Vec<iatfm_core::dashboard::NativeProfileConstraint>> {
        let points=self.points;let reference=self.reference;let pilot=self.pilot;let mut constraints=vec![];
            let id=rad_str(cap,"point");
            let matches=points.iter().filter(|p|p.name==id).collect::<Vec<_>>();
            let mut low=cap["minFt"].as_f64();let mut high=cap["maxFt"].as_f64();
            let rfl=matches.first().and_then(|p|p.minute_offset).and_then(|m|self.altitude_at(f64::from(m),true));
            if cap.get("forbiddenBand").is_some() {
                let a=cap["forbiddenBand"]["minFt"].as_f64();
                let b=cap["forbiddenBand"]["maxFt"].as_f64();
                let raw=matches.first().and_then(|p|p.altitude_ft);
                if let (Some(a),Some(b),Some(raw),Some(rfl))=(a,b,raw,rfl) {
                    if !a.is_finite() || !b.is_finite() || !raw.is_finite() || a<0. || a>=b {
                        return None;
                    }
                    if raw>=b || (raw>a && b<=rfl && b-raw<raw-a) {low=Some(b);high=None;}
                    else {low=None;high=Some(a);}
                } else {return None;}
            }
            if matches.len()!=1 || rfl.is_none() || (low.is_none() && high.is_none())
                || low.is_some_and(|v|!v.is_finite() || v<0. || v>rfl.unwrap())
                || high.is_some_and(|v|!v.is_finite() || v<0.) {
                return None;
            }
            constraints.push(iatfm_core::dashboard::NativeProfileConstraint {
                point:id.into(),min_ft:low,max_ft:Some(high.map_or(rfl.unwrap(),|h|h.min(rfl.unwrap()))),through:None,distance_span:None,
            });
            let base=constraints.len()-1;
            let mut extension_failed=false;
            for extension in cap["extensionTerminals"].as_array().into_iter().flatten() {
                match self.eval(&extension["condition"]) {
                    RadTruth::No=>continue,
                    RadTruth::Unknown=>{extension_failed=true;break;},
                    RadTruth::Yes=>{},
                }
                let plan=pilot.flight_plan.as_ref();
                let airport=match rad_str(extension,"terminal") {
                    "dep"=>plan.and_then(|p|p.departure_id.as_deref()),
                    "arr"=>plan.and_then(|p|p.arrival_id.as_deref()),
                    _=>None,
                };
                let ad=airport.and_then(|id|reference.navdata.airports.get(id));
                let Some(ad)=ad else {extension_failed=true;break;};
                let Some(position)=matches[0].position else {extension_failed=true;break;};
                let distance=iatfm_core::weather::distance_nm(ad.position,position)*1.852;
                if !distance.is_finite() {extension_failed=true;break;}
                if distance>=200. {continue;}
                let airport=airport.unwrap();
                let positions=points.iter().enumerate().filter(|(_,p)|p.name==airport).map(|(i,_)|i).collect::<Vec<_>>();
                if positions.len()!=1 || high.is_none() {extension_failed=true;break;}
                let target=points.iter().position(|p|p.name==id).unwrap();
                let (a,b)=(positions[0].min(target),positions[0].max(target));
                for point in &points[a..=b] {
                    if point.name==id {continue;}
                    if reference.navdata.points.contains_key(&point.name) || reference.navdata.airports.contains_key(&point.name) {
                        constraints.push(iatfm_core::dashboard::NativeProfileConstraint {
                            point:point.name.clone(),min_ft:None,max_ft:high,through:None,distance_span:None,
                        });
                    }
                }
                constraints.push(iatfm_core::dashboard::NativeProfileConstraint {
                    point:id.into(),min_ft:None,max_ft:high,through:Some(airport.into()),distance_span:None,
                });
            }
            if extension_failed {constraints.truncate(base);return None;}
        Some(constraints)
    }

    fn database_profile_distance(&self,t:f64)->Option<f64> {
        self.points.windows(2).find_map(|p| {
            let (x,y,da,db)=(f64::from(p[0].minute_offset?),f64::from(p[1].minute_offset?),p[0].distance_nm?,p[1].distance_nm?);
            (x<=t && t<=y && x<y && da<=db).then(||da+(db-da)*(t-x)/(y-x))
        })
    }

    fn database_route_profile_constraints(&mut self,cap:&RadValue)->Option<Vec<iatfm_core::dashboard::NativeProfileConstraint>> {
        let (events,unknown)=self.events(&cap["routeLocation"]);
        if unknown {return None;}
        let low=cap["minFt"].as_f64().unwrap_or(0.);let high=cap["maxFt"].as_f64().unwrap_or(f64::INFINITY);
        if !low.is_finite() || low<0. || high.is_nan() || low>high {return None;}
        let mut result=vec![];
        for (a,b) in events {
            if a>=b {return None;}
            let (da,db)=(self.database_profile_distance(a)?,self.database_profile_distance(b)?);
            let mut times=vec![a,b];times.extend(self.points.iter().filter_map(|p|p.minute_offset).map(f64::from).filter(|t|*t>a && *t<b));
            let rfl=times.iter().map(|t|self.altitude_at(*t,true)).collect::<Option<Vec<_>>>()?;
            let raw=times.iter().map(|t|self.altitude_at(*t,false)).collect::<Option<Vec<_>>>()?;
            if rfl.iter().chain(&raw).any(|h|!h.is_finite()) {return None;}
            let (min_ft,max_ft)=match rad_str(cap,"mode") {
                "MANDATORY"=>{
                    if rfl.iter().any(|h|*h<low) {return None;}
                    (Some(low),high.is_finite().then_some(high))
                },
                "FORBIDDEN"=>{
                    let above=high.is_finite() && (low==0. || raw.iter().all(|h|*h>=high)) && rfl.iter().all(|h|*h>=high);
                    if low==0. && !above {return None;}
                    if above {(Some(high),None)}else{(None,Some(low))}
                },_=>return None,
            };
            result.push(iatfm_core::dashboard::NativeProfileConstraint{point:String::new(),min_ft,max_ft,through:None,distance_span:Some((da,db))});
        }
        Some(result)
    }

    fn database_airspace_profile_constraints(&mut self,cap:&RadValue)
        ->Option<Vec<iatfm_core::dashboard::NativeProfileConstraint>> {
        let id=rad_str(cap,"airspace");
        let mut sectors=self.reference.sectors.iter().filter(|s|s.id.as_deref()==Some(id));
        let sector=sectors.next()?;
        if sectors.next().is_some() || sector.blocks.is_empty() {return None;}
        let blocks=sector.blocks.iter().map(|b|(b.floor_ft,b.ceiling_ft)).collect::<Vec<_>>();
        let mandatory=cap.get("mandatoryAirspaceBand").is_some();
        let mut constraints=vec![];
        for (index,(floor,ceiling)) in blocks.into_iter().enumerate() {
            let band=&cap[if mandatory {"mandatoryAirspaceBand"}else{"forbiddenAirspaceBand"}];
            let low=band["min"].as_f64().or_else(||match rad_str(band,"min") {
                "GROUND"=>Some(0.),"FLOOR"=>floor,_=>None,
            })?;
            let high=band["max"].as_f64().or_else(||match rad_str(band,"max") {
                "CEILING"=>ceiling,"UNLIMITED"=>Some(f64::INFINITY),_=>None,
            })?;
            if !low.is_finite() || low<0. || high.is_nan() {return None;}
            if low>=high {continue;}
            let (events,unknown)=self.events(&rad_json!({"op":"airspaceFootprint","values":[id],"blockIndex":index}));
            if unknown {return None;}
            for (a,b) in events {
                if a>=b {continue;}
                let distance_at=|t:f64|->Option<f64> {
                    self.points.windows(2).find_map(|p| {
                        let (x,y,da,db)=(f64::from(p[0].minute_offset?),f64::from(p[1].minute_offset?),p[0].distance_nm?,p[1].distance_nm?);
                        (x<=t && t<=y && x<y && da<=db).then(||da+(db-da)*(t-x)/(y-x))
                    })
                };
                let (mut da,mut db)=(distance_at(a)?,distance_at(b)?);
                if !da.is_finite() || !db.is_finite() || da<0. || da>=db {return None;}
                if mandatory {
                    // Existential passage: use an interior anchor, never constrain
                    // the complete traversal as though this were a route band.
                    let mut anchors=vec![(a+b)/2.];
                    anchors.extend(self.points.iter().filter_map(|p|p.minute_offset).map(f64::from).filter(|t|*t>a && *t<b));
                    for t in anchors {
                        let rfl=self.altitude_at(t,true)?;
                        if !rfl.is_finite() {return None;}
                        if rfl<low {continue;}
                        let d=distance_at(t)?;
                        return Some(vec![iatfm_core::dashboard::NativeProfileConstraint {
                            point:String::new(),min_ft:Some(low),max_ft:Some(high.min(rfl)),through:None,distance_span:Some((d,d)),
                        }]);
                    }
                    continue;
                }
                let mut times=vec![a,b];times.extend(self.points.iter().filter_map(|p|p.minute_offset)
                    .map(f64::from).filter(|t|*t>a && *t<b));
                let heights=times.iter().map(|t|self.altitude_at(*t,false)).collect::<Option<Vec<_>>>()?;
                let requested=times.iter().map(|t|self.altitude_at(*t,true)).collect::<Option<Vec<_>>>()?;
                if heights.iter().chain(&requested).any(|h|!h.is_finite()) {return None;}
                // Preserve an already valid upper passage. Otherwise prefer
                // the lower permitted side, subject to profile feasibility.
                let above=high.is_finite() && (low==0. || heights.iter().all(|h|*h>=high)) && requested.iter().all(|h|*h>=high);
                if low==0. && !above {return None;}
                let (min_ft,max_ft)=if above {(Some(high),None)}else{(None,Some(low))};
                if let Some(extensions)=cap["extensionTerminals"].as_array() {
                    if high.is_finite() {return None;}
                    for extension in extensions {
                        match self.eval(&extension["condition"]) {RadTruth::No=>continue,RadTruth::Unknown=>return None,RadTruth::Yes=>{}}
                        let plan=self.pilot.flight_plan.as_ref()?;
                        let (airport,endpoint)=match rad_str(extension,"terminal") {
                            "dep"=>(plan.departure_id.as_deref()?,self.points.first()?),
                            "arr"=>(plan.arrival_id.as_deref()?,self.points.last()?),_=>return None,
                        };
                        if endpoint.name!=airport {return None;}
                        let position=self.reference.navdata.airports.get(airport)?.position;
                        let polygon=&self.reference.sectors.iter().find(|s|s.id.as_deref()==Some(id))?.blocks[index].polygon;
                        let ring=Polygon::new(LineString::new(polygon.iter().map(|p|Coord{x:p.lon,y:p.lat}).collect()),vec![]);
                        let p=geo::Point::new(position.lon,position.lat);
                        let distance=if ring.contains(&p) {0.}else{
                            let mut nearest=f64::INFINITY;
                            for i in 0..polygon.len() {nearest=nearest.min(rad_native_arc_distance_nm(position,polygon[i],polygon[(i+1)%polygon.len()])?);}
                            nearest*1.852
                        };
                        if distance<200. {
                            let d=endpoint.distance_nm?;
                            if rad_str(extension,"terminal")=="dep" {da=da.min(d);}else{db=db.max(d);}
                        }
                    }
                }
                constraints.push(iatfm_core::dashboard::NativeProfileConstraint {
                    point:String::new(),min_ft,max_ft,through:None,distance_span:Some((da,db)),
                });
            }
        }
        if mandatory {None}else{Some(constraints)}
    }
}
