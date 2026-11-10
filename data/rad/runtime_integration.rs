// Native NMP state periods are half-open UTC intervals. Keep every state that
// can intersect the trajectory, then check validity at its reference location.
fn rad_native_rule_pending(rule: &RadValue) -> bool {
    fn unresolved(value: &RadValue) -> bool {
        match value {
            RadValue::Object(map) => rad_str(value, "op") == "unknown" || map.values().any(unresolved),
            RadValue::Array(values) => values.iter().any(unresolved),
            _ => false,
        }
    }
    rule["conversionDiagnostics"].as_array().is_some_and(|a| !a.is_empty())
        || ["scope", "schedule", "referenceLocation", "clauses"].iter().any(|k| unresolved(&rule[*k]))
}

fn rad_database_state_overlaps(rule: &RadValue, takeoff: DateTime<Utc>, arrival: Option<DateTime<Utc>>) -> bool {
    let period = &rule["sourceValidity"];
    match (period["start"].as_i64(), period["end"].as_i64()) {
        (Some(start), Some(end)) if start < end =>
            end > takeoff.timestamp_millis()
                && arrival.is_none_or(|a| start <= a.timestamp_millis()),
        _ => true, // Malformed validity remains unresolved during evaluation.
    }
}

fn rad_database_interval(at: DateTime<Utc>, period: &RadValue) -> RadTruth {
    match (period["start"].as_i64(), period["end"].as_i64()) {
        (Some(start), Some(end)) if start < end =>
            rad_bool(start <= at.timestamp_millis() && at.timestamp_millis() < end),
        _ => RadTruth::Unknown,
    }
}

fn rad_schedule_has_own_reference(node: &RadValue) -> bool {
    match rad_str(node, "op") {
        "true" | "false" | "timeAt" => true,
        "and" | "or" | "not" => node["args"].as_array().is_some_and(|a|
            !a.is_empty() && a.iter().all(rad_schedule_has_own_reference)),
        _ => false,
    }
}

fn rad_native_classification(actual: Option<&str>, patterns: &[&str]) -> RadTruth {
    let Some(actual) = actual else { return RadTruth::Unknown; };
    let a = actual.as_bytes();
    let alphabets: [&[u8]; 3] = [b"LSAHGT", b"123468C", b"PTJE"];
    if a.len() != 3 || !a.iter().enumerate().all(|(i,b)| alphabets[i].contains(b))
        || patterns.is_empty() || patterns.iter().any(|p|p.len()!=3
            || !p.bytes().enumerate().all(|(i,b)|b==b'?' || b==b'*' || alphabets[i].contains(&b))) {
        return RadTruth::Unknown;
    }
    rad_bool(patterns.iter().any(|p|p.bytes().zip(a).all(|(p,a)|p==b'?' || p==b'*' || p==*a)))
}

impl RadContext<'_> {
    fn database_route_availability(&self,node:&RadValue)->RadTruth {
        if !rad_support_valid(&self.support,self.at) {return RadTruth::Unknown;}
        let record=&self.support["nativeRouteAvailability"][rad_str(node,"contextKey")];
        let (Some(before),Some(after),Some(low))=(node["offsetStartMinutes"].as_i64(),
            node["offsetEndMinutes"].as_i64(),node["minFt"].as_f64()) else {return RadTruth::Unknown;};
        let high=if node["maxFt"].is_null(){f64::INFINITY}else{node["maxFt"].as_f64().unwrap_or(f64::NAN)};
        if !(-90..=90).contains(&before) || !(-90..=90).contains(&after) || !low.is_finite() || high.is_nan() || low>high
            || record["complete"]!=true || rad_str(record,"source").is_empty()
            || ["sourceRouteRef","fromPointRef","toPointRef"].iter().any(|k|record[*k]!=node[*k])
            || !rad_support_valid(record,self.at-ChronoDuration::minutes(before))
            || !rad_support_valid(record,self.at-ChronoDuration::minutes(after)) {return RadTruth::Unknown;}
        let Some(segments)=record["segments"].as_array().filter(|s|!s.is_empty()) else {return RadTruth::Unknown;};
        if segments[0]["fromPointRef"]!=node["fromPointRef"] || segments.last().unwrap()["toPointRef"]!=node["toPointRef"]
            || segments.windows(2).any(|s|s[0]["toPointRef"]!=s[1]["fromPointRef"]) {return RadTruth::Unknown;}
        let mut result=RadTruth::Yes;
        for segment in segments {
            let Some(windows)=segment["windows"].as_array() else {result=result.and(RadTruth::Unknown);continue;};
            let mut available=RadTruth::No;
            for window in windows {
                let (Ok(a),Ok(b),Some(min),Some(max))=(rad_str(window,"validFrom").parse::<DateTime<Utc>>(),
                    rad_str(window,"validUntil").parse::<DateTime<Utc>>(),window["minFt"].as_f64(),window["maxFt"].as_f64())
                    else {available=available.or(RadTruth::Unknown);continue;};
                if a>=b || !min.is_finite() || !max.is_finite() || min>max {available=available.or(RadTruth::Unknown);continue;}
                if self.at>=a+ChronoDuration::minutes(before) && self.at<b+ChronoDuration::minutes(after)
                    && min<=high && max>=low {available=RadTruth::Yes;}
            }
            // NM: outside availability means all levels closed in at least
            // one segment; the available level need not be the flight's RFL.
            result=result.and(available);
        }
        result
    }

    fn database_cruise_at(&mut self,node:&RadValue)->RadTruth {
        let (spans,mut unknown)=self.events(&node["location"]);
        unknown|=!self.route_complete() || self.points.iter().any(|p|
            p.minute_offset.is_none() || p.altitude_ft.is_none_or(|h|!h.is_finite()));
        for (start,end) in spans {
            if start>=end {unknown=true;continue;}
            let mut covered=start;
            for pair in self.points.windows(2) {
                let (Some(a),Some(b))=(pair[0].minute_offset,pair[1].minute_offset) else {unknown=true;continue;};
                let (a,b)=(f64::from(a),f64::from(b));
                if a.max(start)>=b.min(end) {continue;}
                if a>covered+1e-7 {unknown=true;}
                covered=covered.max(b.min(end));
                let (Some(ha),Some(hb))=(pair[0].altitude_ft,pair[1].altitude_ft) else {unknown=true;continue;};
                if !ha.is_finite() || !hb.is_finite() {unknown=true;continue;}
                // Equality is numerical tolerance only, not a vertical-speed
                // threshold that would turn a shallow climb into cruising.
                if (ha-hb).abs()<1e-7 {return RadTruth::Yes;}
            }
            if covered<end-1e-7 {unknown=true;}
        }
        if unknown {RadTruth::Unknown}else{RadTruth::No}
    }

    fn database_procedure_events(&self,node:&RadValue,first_only:bool)->(Vec<(f64,f64)>,bool) {
        let ids=rad_values(node);
        if ids.len()!=1 {return (vec![],true);}
        let id=ids[0];let filed=self.route_tokens();
        let occurrences=filed.iter().enumerate().filter(|(_,v)|v.as_str()==id).map(|(i,_)|i).collect::<Vec<_>>();
        if occurrences.is_empty() {return (vec![],!self.route_complete());}
        if occurrences.len()!=1 {return (vec![],true);}
        let Some(plan)=self.pilot.flight_plan.as_ref() else {return (vec![],true);};
        let actual=self.points.iter().filter(|p|self.reference.navdata.points.contains_key(&p.name)
            || !(p.name=="TOC" || p.name=="TOD" || p.name.starts_with("TOC STEP") || p.name.starts_with("TOD STEP") || p.name.starts_with("NMP PTR BOUNDARY ")))
            .collect::<Vec<_>>();
        let mut events=vec![];let mut found=false;
        for (airport,kind,valid_position) in [(plan.departure_id.as_deref(),"SID",occurrences[0]==1),
            (plan.arrival_id.as_deref(),"STAR",occurrences[0]+2==filed.len())] {
            if !valid_position {continue;}
            let Some(airport)=airport else {continue;};
            for procedure in self.reference.navdata.procedures.get(airport).into_iter().flatten()
                .filter(|p|p.name==id && p.procedure_type==kind) {
                // A leg without a fix has no exact reference event in this
                // navdata representation. Do not invent its time from a later fix.
                if procedure.legs.is_empty() || procedure.legs.iter().any(|l|l.fix.as_deref().is_none_or(str::is_empty)) {continue;}
                let mut fixes=procedure.legs.iter().filter_map(|l|l.fix.as_deref()).collect::<Vec<_>>();
                fixes.dedup();
                if kind=="STAR" && first_only && fixes.len()<2 {continue;}
                for window in actual.windows(fixes.len()) {
                    if !window.iter().zip(&fixes).all(|(p,id)|p.name==*id) {continue;}
                    let first=window[0].minute_offset;let last=window.last().unwrap().minute_offset;
                    let (start,end)=if kind=="SID" {
                        let departure=self.points.first().and_then(|p|p.minute_offset);
                        (departure,if first_only {window.iter().filter_map(|p|p.minute_offset)
                            .find(|t|departure.is_some_and(|d|*t>d))}else{last})
                    } else {
                        (first,if first_only {window.get(1).and_then(|p|p.minute_offset)
                            .or_else(||self.points.last().and_then(|p|p.minute_offset))}
                            else {self.points.last().and_then(|p|p.minute_offset)})
                    };
                    if let (Some(a),Some(b))=(start,end) {
                        if a<b {events.push((f64::from(a),f64::from(b)));found=true;}
                    }
                }
            }
        }
        events.sort_by(|a,b|a.0.total_cmp(&b.0).then(a.1.total_cmp(&b.1)));events.dedup();
        if events.len()>1 {return (vec![],true);}
        (events,!found || !self.route_complete())
    }

    fn database_relative_slice_events(&mut self,node:&RadValue)->(Vec<(f64,f64)>,bool) {
        let ids=rad_values(node);
        if ids.len()!=1 {return (vec![],true);}
        let mut sectors=self.reference.sectors.iter().filter(|s|s.id.as_deref()==Some(ids[0]));
        let Some(sector)=sectors.next() else {return (vec![],true);};
        if sectors.next().is_some() || sector.blocks.is_empty() {return (vec![],true);}
        let bounds=sector.blocks.iter().map(|b|(b.floor_ft,b.ceiling_ft)).collect::<Vec<_>>();
        if rad_str(node,"min")=="CEILING" || rad_str(node,"max")=="FLOOR" {
            // Inverse slices use the envelope of every overlapping block:
            // below the lowest floor / above the highest ceiling, not a union
            // of the complements of the individual block volumes.
            let mut location=rad_json!({"op":"airspaceFootprint","values":ids});
            for (key,comparison) in [("min","ABV"),("max","BLW")] {
                if let Some(value)=node[key].as_f64() {location=rad_json!({"op":"level","location":location,"comparison":comparison,"value":value,"requested":node["requested"]==true});}
            }
            let (mut candidates,mut unknown)=self.events(&location);
            for (index,(floor,ceiling)) in bounds.into_iter().enumerate() {
                let footprint=rad_json!({"op":"airspaceFootprint","values":ids,"blockIndex":index});
                let (inside,incomplete)=self.events(&footprint);unknown|=incomplete;
                for (key,symbol,bound,comparison) in [("min","CEILING",ceiling,"ABV"),("max","FLOOR",floor,"BLW")] {
                    if rad_str(node,key)!=symbol {continue;}
                    let Some(bound)=bound.filter(|v|v.is_finite()) else {return (vec![],true);};
                    let (valid,incomplete)=self.events(&rad_json!({"op":"level","location":footprint,
                        "comparison":comparison,"value":bound,"requested":node["requested"]==true}));
                    unknown|=incomplete;
                    let mut outside=candidates.clone();
                    for &(start,end) in &inside {
                        outside=outside.into_iter().flat_map(|(a,b)| {
                            if end<a || start>b {return vec![(a,b)];}
                            let mut parts=vec![];
                            if a<start {parts.push((a,start));}
                            if end<b {parts.push((end,b));}
                            parts
                        }).collect();
                    }
                    for &(a,b) in &candidates {
                        for &(c,d) in &valid {
                            let (start,end)=(a.max(c),b.min(d));
                            if start<=end {outside.push((start,end));}
                        }
                    }
                    candidates=rad_merge_spans(outside);
                }
            }
            return (candidates,unknown);
        }
        let mut events=vec![];let mut unknown=false;
        for (index,(floor,ceiling)) in bounds.into_iter().enumerate() {
            let resolve=|key:&str|->Option<Option<f64>> {
                if let Some(n)=node[key].as_f64().filter(|n|n.is_finite()) {return Some(Some(n));}
                match (key,rad_str(node,key)) {
                    ("min","GROUND")|("max","UNLIMITED")=>Some(None),
                    ("min","FLOOR")=>floor.filter(|n|n.is_finite()).map(Some),
                    ("max","CEILING")=>ceiling.filter(|n|n.is_finite()).map(Some),
                    _=>None,
                }
            };
            let (Some(low),Some(high))=(resolve("min"),resolve("max")) else {unknown=true;continue;};
            if low.zip(high).is_some_and(|(a,b)|a>=b) {continue;}
            let mut location=rad_json!({"op":"airspaceFootprint","values":ids,"blockIndex":index});
            for (comparison,value) in [("ABV",low),("BLW",high)] {
                if let Some(value)=value {
                    location=rad_json!({"op":"level","location":location,"comparison":comparison,
                        "value":value,"requested":node["requested"]==true});
                }
            }
            let (spans,incomplete)=self.events(&location);events.extend(spans);unknown|=incomplete;
        }
        (rad_merge_spans(events),unknown)
    }

    fn database_registration_prefix(&self,node:&RadValue)->RadTruth {
        let Some(plan)=self.pilot.flight_plan.as_ref() else {return RadTruth::Unknown;};
        let normalize=|s:&str|s.trim().to_ascii_uppercase().replace('-',"");
        let field=plan.registration.as_deref().map(normalize);
        let remarks=plan.remarks.as_deref().and_then(|s|s.split_whitespace().find_map(|p|p.strip_prefix("REG/"))).map(normalize);
        if field.is_some() && remarks.is_some() && field!=remarks {return RadTruth::Unknown;}
        let Some(actual)=field.or(remarks).filter(|s|!s.is_empty() && s.bytes().all(|b|b.is_ascii_alphanumeric()))
            else {return RadTruth::Unknown;};
        let prefixes=rad_values(node);
        if prefixes.is_empty() || prefixes.iter().any(|p|p.is_empty() || !p.bytes().all(|b|b.is_ascii_alphanumeric())) {
            return RadTruth::Unknown;
        }
        rad_bool(prefixes.iter().any(|p|actual.starts_with(*p)))
    }

    fn database_point_list_events(&self,node:&RadValue)->(Vec<(f64,f64)>,bool) {
        // Resolve the complete route portion, independently of the filed airway
        // name. An ambiguous graph must never be replaced by a shortest path.
        use std::collections::{BTreeMap,BTreeSet,VecDeque};
        let (Some(start),Some(route),Some(end))=(node["parts"][0][0].as_str(),
            node["parts"][1][0].as_str(),node["parts"][2][0].as_str()) else {return (vec![],true);};
        if start==end {return (vec![],true);}
        let nav=&self.reference.navdata;
        let mut graph=BTreeMap::<String,BTreeSet<String>>::new();
        let mut edge=|a:&str,b:&str| {
            graph.entry(a.into()).or_default().insert(b.into());
            graph.entry(b.into()).or_default().insert(a.into());
        };
        if let Some(segments)=nav.airway_segments.get(route) {
            for s in segments {edge(&s.from,&s.to);}
        } else if let Some(points)=nav.airways.get(route) {
            for pair in points.windows(2) {edge(&pair[0],&pair[1]);}
        } else {return (vec![],true);}
        let find=|excluded:Option<(&str,&str)>|->Option<Vec<String>> {
            let mut queue=VecDeque::from([start.to_string()]);
            let mut previous=BTreeMap::from([(start.to_string(),start.to_string())]);
            while let Some(a)=queue.pop_front() {
                if a==end {
                    let mut path=vec![a];
                    while path.last().unwrap()!=start {path.push(previous[path.last().unwrap()].clone());}
                    path.reverse();return Some(path);
                }
                for b in graph.get(&a).into_iter().flatten() {
                    if excluded.is_some_and(|(x,y)|(a==x && b==y)||(a==y && b==x)) {continue;}
                    if !previous.contains_key(b) {previous.insert(b.clone(),a.clone());queue.push_back(b.clone());}
                }
            }
            None
        };
        let Some(expected)=find(None) else {return (vec![],true);};
        if expected.windows(2).any(|p|find(Some((&p[0],&p[1]))).is_some()) {return (vec![],true);}
        if expected.iter().any(|id|!nav.points.contains_key(id)
            || nav.point_candidates.get(id).is_some_and(|p|p.len()>1)) {return (vec![],true);}
        // Remove only profile-generated climb/descent markers. Coordinates or
        // other real intermediate fixes must remain and prevent a false match.
        let actual=self.points.iter().filter(|p|nav.points.contains_key(&p.name)
            || !(p.name=="TOC" || p.name=="TOD" || p.name.starts_with("TOC STEP") || p.name.starts_with("TOD STEP") || p.name.starts_with("NMP PTR BOUNDARY ")))
            .collect::<Vec<_>>();
        let mut unknown=!self.route_complete();
        let mut events=vec![];
        if node["partial"]==true {
            let filed=self.expanded_route_tokens();
            for pair in actual.windows(2) {
                if !expected.windows(2).any(|p|pair[0].name==p[0] && pair[1].name==p[1]) {continue;}
                if node["requireFiledRoute"]==true && !filed.windows(3)
                    .any(|p|p[0]==pair[0].name && p[1]==route && p[2]==pair[1].name) {continue;}
                match (pair[0].minute_offset,pair[1].minute_offset) {
                    (Some(a),Some(b)) if a<b=>events.push((f64::from(a),f64::from(b))),
                    _=>unknown=true,
                }
            }
            return (rad_merge_spans(events),unknown);
        }
        for window in actual.windows(expected.len()) {
            if !window.iter().zip(&expected).all(|(p,id)|p.name==*id) {continue;}
            match (window[0].minute_offset,window.last().unwrap().minute_offset) {
                (Some(a),Some(b)) if a<b=>events.push((f64::from(a),f64::from(b))),
                _=>unknown=true,
            }
        }
        (events,unknown)
    }

    fn database_terminal_dct_length(&self,node:&RadValue)->RadTruth {
        let Some(limit)=node["value"].as_f64().filter(|v|v.is_finite() && *v>=0. && *v<=998.) else {return RadTruth::Unknown;};
        let tokens=self.route_tokens();
        if tokens.len()<3 {return RadTruth::Unknown;}
        let (airport,mut neighbor,step)=match rad_str(node,"terminal") {
            "dep"=>(0,1,1isize),
            "arr"=>(tokens.len()-1,tokens.len()-2,-1isize),
            _=>return RadTruth::Unknown,
        };
        if self.procedure_ids.contains(&tokens[neighbor]) {
            let kind=if rad_str(node,"terminal")=="dep" {"SID"}else{"STAR"};
            return if self.reference.navdata.procedures.get(&tokens[airport]).is_some_and(|procedures|
                procedures.iter().any(|p|p.name==tokens[neighbor] && p.procedure_type==kind)) {
                RadTruth::No
            } else {RadTruth::Unknown};
        }
        if tokens[neighbor]=="DCT" {
            neighbor=match neighbor.checked_add_signed(step).filter(|i|*i<tokens.len()) {
                Some(i)=>i,None=>return RadTruth::Unknown,
            };
        }
        if let Some(allowed)=node.get("allowedPoints") {
            let Some(allowed)=allowed.as_array() else {return RadTruth::Unknown;};
            if allowed.iter().any(|v|v.as_str().is_none_or(str::is_empty)) {return RadTruth::Unknown;}
            if allowed.iter().any(|v|v.as_str()==Some(tokens[neighbor].as_str())) {return RadTruth::No;}
        }
        let Some(ad)=self.reference.navdata.airports.get(&tokens[airport]) else {return RadTruth::Unknown;};
        let endpoint=self.reference.navdata.points.get(&tokens[neighbor]).copied()
            .or_else(||self.reference.navdata.airports.get(&tokens[neighbor]).map(|a|a.position));
        let Some(endpoint)=endpoint else {return RadTruth::Unknown;};
        let distance=iatfm_core::weather::distance_nm(ad.position,endpoint);
        if !distance.is_finite() {RadTruth::Unknown}else{rad_bool(distance>limit)}
    }

    fn database_fra_border_events(&mut self,node:&RadValue)->(Vec<(f64,f64)>,bool) {
        let Some(borders)=node["borders"].as_array().filter(|v|!v.is_empty()) else {return (vec![],true);};
        if !self.route_complete() || self.points.iter().any(|p|p.altitude_ft.is_none_or(|h|!h.is_finite())
            || p.position.is_none_or(|p|!p.lat.is_finite() || !p.lon.is_finite())) {return (vec![],true);}
        let mut events=vec![];let mut unknown=false;
        for border in borders {
            for key in ["from","to"] {
                let id=rad_str(border,key);
                let sectors=self.reference.sectors.iter().filter(|s|s.id.as_deref()==Some(id)).collect::<Vec<_>>();
                if sectors.len()!=1 || sectors[0].blocks.is_empty() || sectors[0].blocks.iter().any(|b|
                    b.floor_ft.is_none_or(|n|!n.is_finite()) || b.ceiling_ft.is_none_or(|n|!n.is_finite())) {return (vec![],true);}
            }
            let (spans,u)=self.events(&rad_json!({"op":"boundary","from":{"op":"via","values":[border["from"]]},
                "to":{"op":"via","values":[border["to"]]},"bidirectional":false}));
            for span in spans {
                if let Some(band)=node.get("crossingBand") {
                    let Some(h)=self.altitude_at(span.0,false) else {unknown=true;continue;};
                    if band["minFt"].as_f64().is_some_and(|v|h<v) || band["maxFt"].as_f64().is_some_and(|v|h>v) {continue;}
                }
                events.push(span);
            }
            unknown|=u;
        }
        (rad_merge_spans(events),unknown)
    }

    fn database_fra_border_violation(&mut self,node:&RadValue)->RadTruth {
        let enroute=rad_str(node,"op")=="nativeDctViolation";
        let (events,unknown)=if enroute {self.database_dct_area_events(node)}else{self.database_fra_border_events(node)};
        let mut result=if unknown {RadTruth::Unknown}else{RadTruth::No};
        let tokens=self.expanded_route_tokens();
        let known_point=|id:&str|self.points.iter().any(|p|p.name==id);
        // Explicit ATS/procedure legs are not DCT. Adjacent fixes without a
        // connector are implicit DCT; ambiguous/repeated occurrences stay unknown.
        let mut legs=vec![];
        for w in tokens.windows(3) {
            if known_point(&w[0]) && known_point(&w[2]) {
                if w[1]=="DCT" {legs.push((w[0].clone(),w[2].clone(),true));}
                else if self.reference.navdata.airways.contains_key(&w[1]) || self.reference.navdata.airway_segments.contains_key(&w[1]) || self.procedure_ids.contains(&w[1]) {
                    legs.push((w[0].clone(),w[2].clone(),false));
                }
            }
        }
        for w in tokens.windows(2) {if known_point(&w[0]) && known_point(&w[1]) {legs.push((w[0].clone(),w[1].clone(),true));}}
        for (minute,finish) in events {
            let mut crossing=RadTruth::No;let mut found=false;
            for (a,b,direct) in &legs {
                let starts=self.points.iter().filter(|p|p.name==*a).collect::<Vec<_>>();
                let ends=self.points.iter().filter(|p|p.name==*b).collect::<Vec<_>>();
                if starts.len()!=1 || ends.len()!=1 {crossing=crossing.or(RadTruth::Unknown);continue;}
                let (Some(start),Some(end))=(starts[0].minute_offset,ends[0].minute_offset) else {crossing=crossing.or(RadTruth::Unknown);continue;};
                if f64::from(start)>finish+1e-7 || f64::from(end)<minute-1e-7 || start>=end {continue;}
                found=true;
                if !direct {continue;}
                let mut allowed=node["entryExitPoints"].as_array().is_some_and(|ps|ps.iter().any(|p|p.as_str()==Some(a) || p.as_str()==Some(b)))
                    || node["allowedDctSegments"].as_array().is_some_and(|ss|ss.iter().any(|s|s[0].as_str()==Some(a) && s[1].as_str()==Some(b)));
                let in_band=|h:f64,v:&RadValue| h.is_finite() && v["minFt"].as_f64().is_none_or(|v|h>=v) && v["maxFt"].as_f64().is_none_or(|v|h<=v);
                for option in node["pointOptions"].as_array().into_iter().flatten() {
                    for p in [starts[0],ends[0]] {
                        if option["point"].as_str()==Some(&p.name) && p.altitude_ft.is_some_and(|h|in_band(h,option)) {allowed=true;}
                    }
                }
                for option in node["segmentOptions"].as_array().into_iter().flatten() {
                    if option["from"].as_str()==Some(a) && option["to"].as_str()==Some(b) {
                        let heights=self.points.iter().filter(|p|p.minute_offset.is_some_and(|t|t>=start && t<=end));
                        if heights.into_iter().all(|p|p.altitude_ft.is_some_and(|h|in_band(h,option))) {allowed=true;}
                    }
                }
                let violation=if enroute && rad_str(node,"mode")=="FORBIDDEN" {allowed}
                    else if enroute {
                        let distance=iatfm_core::weather::distance_nm(starts[0].position.unwrap(),ends[0].position.unwrap());
                        !allowed && node["limitNm"].as_f64().is_some_and(|limit|distance>limit)
                    }else{!allowed};
                if !violation {continue;}
                let interval=rad_json!({"op":"and","args":[node["schedule"],{"op":"utcInterval",
                    "start":node["sourceValidity"]["start"],"end":node["sourceValidity"]["end"]}]});
                let (a,b)=(minute.max(f64::from(start)),finish.min(f64::from(end)));
                let mut instants=vec![a,b];
                if a<b {instants.extend(self.database_schedule_samples(a,b,&interval).into_iter().map(|s|s as f64/60.));}
                let original=self.at;let mut applicable=RadTruth::No;
                for t in instants {
                    if t<a || t>b {continue;}
                    self.at=self.takeoff+ChronoDuration::milliseconds((t*60000.).round() as i64);
                    applicable=applicable.or(self.eval(&interval));
                }
                self.at=original;
                crossing=crossing.or(applicable);
            }
            if !found {crossing=RadTruth::Unknown;}
            result=result.or(crossing);
        }
        result
    }

    fn database_dct_area_events(&mut self,node:&RadValue)->(Vec<(f64,f64)>,bool) {
        if !self.route_complete() || self.points.iter().any(|p|p.altitude_ft.is_none_or(|h|!h.is_finite())) {return (vec![],true);}
        let id=rad_str(node,"airspace");
        let sectors=self.reference.sectors.iter().filter(|s|s.id.as_deref()==Some(id)).collect::<Vec<_>>();
        if sectors.len()!=1 || sectors[0].blocks.is_empty() || sectors[0].blocks.iter().any(|b|b.floor_ft.is_none() || b.ceiling_ft.is_none()) {return (vec![],true);}
        let (spans,unknown)=self.events(&rad_json!({"op":"via","values":[id]}));
        let band=&node["crossingBand"];let low=band["minFt"].as_f64().unwrap_or(f64::NEG_INFINITY);
        let high=band["maxFt"].as_f64().unwrap_or(f64::INFINITY);let mut out=vec![];
        for (a,b) in spans {
            for p in self.points.windows(2) {
                let (x,y)=(f64::from(p[0].minute_offset.unwrap()),f64::from(p[1].minute_offset.unwrap()));
                if x>=y {continue;}
                let (h,k)=(p[0].altitude_ft.unwrap(),p[1].altitude_ft.unwrap());
                let (mut start,mut end)=(a.max(x),b.min(y));
                if h==k {if h<low || h>high {continue;}}
                else {
                    let (l,u)=(x+(low-h)*(y-x)/(k-h),x+(high-h)*(y-x)/(k-h));
                    start=start.max(l.min(u));end=end.min(l.max(u));
                }
                if start<=end {out.push((start,end));}
            }
        }
        (rad_merge_spans(out),unknown)
    }

    fn database_dct_at_reference(&mut self,node:&RadValue)->RadTruth {
        let (events,unknown)=self.events(&node["location"]);
        let tokens=self.route_tokens();
        let mut result=if unknown || !self.route_complete(){RadTruth::Unknown}else{RadTruth::No};
        for (start,end) in events {
            if start!=end {result=result.or(RadTruth::Unknown);continue;}
            for point in self.points.iter().filter(|p|p.minute_offset.is_some_and(|t|(f64::from(t)-start).abs()<1e-7)) {
                let positions=tokens.iter().enumerate().filter(|(_,id)|**id==point.name).map(|(i,_)|i).collect::<Vec<_>>();
                if positions.len()!=1 {result=result.or(RadTruth::Unknown);continue;}
                let index=positions[0];
                let direct=match rad_str(node,"direction") {
                    "entry"=>index>=2 && tokens[index-1]=="DCT",
                    "exit"=>index+2<tokens.len() && tokens[index+1]=="DCT",
                    _=>return RadTruth::Unknown,
                };
                result=result.or(rad_bool(direct));
            }
        }
        result
    }

    fn database_first_segment_events(&mut self,node:&RadValue)->(Vec<(f64,f64)>,bool) {
        if rad_str(&node["location"],"op")=="procedureSpan" {
            return self.database_procedure_events(&node["location"],true);
        }
        let (events,mut unknown)=self.events(&node["location"]);
        unknown|=!self.route_complete();
        let result=events.into_iter().map(|(start,end)| {
            let first=self.points.iter().filter(|p|self.reference.navdata.points.contains_key(&p.name))
                .filter_map(|p|p.minute_offset).map(f64::from).filter(|t|*t>start && *t<end)
                .min_by(f64::total_cmp).unwrap_or(end);
            (start,first)
        }).collect();
        (result,unknown)
    }

    fn database_schedule_samples(&self,start:f64,end:f64,schedule:&RadValue)->Vec<i64> {
        fn visit(ctx:&RadContext<'_>,node:&RadValue,out:&mut Vec<i64>) {
            if rad_str(node,"op")=="utcInterval" {
                for key in ["start","end"] {
                    if let Some(ms)=node[key].as_i64().and_then(|t|t.checked_sub(ctx.takeoff.timestamp_millis())) {
                        out.push(ms.div_euclid(1000)+i64::from(ms.rem_euclid(1000)!=0));
                    }
                }
            }
            if rad_str(node,"op")=="nativeRouteAvailability" {
                let record=&ctx.support["nativeRouteAvailability"][rad_str(node,"contextKey")];
                for segment in record["segments"].as_array().into_iter().flatten() {
                    for window in segment["windows"].as_array().into_iter().flatten() {
                        for (key,offset) in [("validFrom","offsetStartMinutes"),("validUntil","offsetEndMinutes")] {
                            if let (Ok(t),Some(shift))=(rad_str(window,key).parse::<DateTime<Utc>>(),node[offset].as_i64().filter(|v|(-90..=90).contains(v))) {
                                let ms=(t+ChronoDuration::minutes(shift)-ctx.takeoff).num_milliseconds();
                                out.push(ms.div_euclid(1000)+i64::from(ms.rem_euclid(1000)!=0));
                            }
                        }
                    }
                }
            }
            if rad_str(node,"op")=="nativeActivation" {
                if let Some(id)=node["sourceRef"]["backendId"].as_str() {
                    for window in ctx.support["nativeActivations"][id]["windows"].as_array().into_iter().flatten() {
                        for (key,offset) in [("validFrom","offsetStartMinutes"),("validUntil","offsetEndMinutes")] {
                            if let (Ok(t),Some(shift))=(rad_str(window,key).parse::<DateTime<Utc>>(),node[offset].as_i64()) {
                                out.push((t+ChronoDuration::minutes(shift)-ctx.takeoff).num_seconds());
                            }
                        }
                        if node["vertical"]==true {
                            for pair in ctx.points.windows(2) {
                                if let (Some(a),Some(b),Some(ha),Some(hb))=(pair[0].minute_offset,pair[1].minute_offset,
                                    pair[0].altitude_ft,pair[1].altitude_ft) {
                                    if b>a && ha!=hb {
                                        for limit in [window["minFt"].as_f64(),window["maxFt"].as_f64()].into_iter().flatten() {
                                            let ratio=(limit-ha)/(hb-ha);
                                            if (0. ..=1.).contains(&ratio) {
                                                let second=((f64::from(a)+ratio*f64::from(b-a))*60.).ceil() as i64;
                                                out.extend([second,second+1]);
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
            match node {
                RadValue::Object(map)=>for v in map.values(){visit(ctx,v,out);},
                RadValue::Array(values)=>for v in values {visit(ctx,v,out);},
                _=>{},
            }
        }
        let mut samples=self.sample_seconds(start,end);
        visit(self,schedule,&mut samples);
        samples.retain(|s|*s as f64>=start*60. && *s as f64<=end*60.);
        samples.sort_unstable();samples.dedup();samples
    }

    fn database_activation(&self,node:&RadValue)->RadTruth {
        if !rad_support_valid(&self.support,self.at) {return RadTruth::Unknown;}
        let Some(id)=node["sourceRef"]["backendId"].as_str() else {return RadTruth::Unknown;};
        let record=&self.support["nativeActivations"][id];
        let (Some(before),Some(after))=(node["offsetStartMinutes"].as_i64(),node["offsetEndMinutes"].as_i64())
            else {return RadTruth::Unknown;};
        if record["sourceRef"]!=node["sourceRef"] || record["complete"]!=true
            || rad_str(record,"source").is_empty()
            || !rad_support_valid(record,self.at-ChronoDuration::minutes(before))
            || !rad_support_valid(record,self.at-ChronoDuration::minutes(after)) {
            return RadTruth::Unknown;
        }
        let Some(windows)=record["windows"].as_array() else {return RadTruth::Unknown;};
        windows.iter().fold(RadTruth::No,|result,window| {
            let (Ok(start),Ok(end))=(rad_str(window,"validFrom").parse::<DateTime<Utc>>(),
                rad_str(window,"validUntil").parse::<DateTime<Utc>>()) else {return result.or(RadTruth::Unknown);};
            if end<=start {return result.or(RadTruth::Unknown);}
            let (start,end)=(start+ChronoDuration::minutes(before),end+ChronoDuration::minutes(after));
            if self.at<start || self.at>=end {return result;}
            if node["fua"]==true {
                // Default Active is an authoring default. Actual FUA activation
                // comes from the restriction list attached to this allocation.
                let Some(ids)=window["restrictionIds"].as_array() else {return result.or(RadTruth::Unknown);};
                if !ids.iter().any(|id|id.as_str()==Some(&self.rule_id)) {return result;}
            }
            if node["vertical"]==true {
                let minute=(self.at-self.takeoff).num_milliseconds() as f64/60000.;
                let (Some(h),Some(low),Some(high))=(self.altitude_at(minute,node["requested"]==true),
                    window["minFt"].as_f64(),window["maxFt"].as_f64()) else {return result.or(RadTruth::Unknown);};
                if !low.is_finite() || !high.is_finite() || low>=high {return result.or(RadTruth::Unknown);}
                result.or(rad_bool(h>=low && h<high))
            } else {result.or(RadTruth::Yes)}
        })
    }

    fn database_airspace_footprint_events(&self, node: &RadValue) -> (Vec<(f64,f64)>,bool) {
        let ids=rad_values(node);
        if ids.is_empty() {return (vec![],true);}
        // Altitude belongs to the slice expression, not to the source volume.
        // A valid slice can be completely below or above that volume.
        let horizontal=self.points.iter().map(|p|iatfm_core::dashboard::VerticalProfilePoint {
            name:p.name.clone(),position:p.position,altitude_ft:Some(0.5),
            minute_offset:p.minute_offset,distance_nm:p.distance_nm,
        }).collect::<Vec<_>>();
        let mut events=vec![];
        let mut unknown=!self.route_complete();
        for id in ids {
            let mut matches=self.reference.sectors.iter().filter(|s|s.id.as_deref()==Some(id));
            let Some(sector)=matches.next() else {unknown=true;continue;};
            if matches.next().is_some() || sector.blocks.is_empty() {unknown=true;continue;}
            if node.get("blockIndex").is_some() && node["blockIndex"].as_u64().is_none_or(|i|i as usize>=sector.blocks.len()) {
                unknown=true;continue;
            }
            for (index,block) in sector.blocks.iter().enumerate() {
                if node["blockIndex"].as_u64().is_some_and(|i|i as usize!=index) {continue;}
                if block.polygon.len()<3 || block.polygon.iter().any(|p|!p.lat.is_finite() || !p.lon.is_finite()) {
                    unknown=true;continue;
                }
                events.extend(rad_crossings(&horizontal,&block.polygon,0.,1.));
            }
        }
        (rad_merge_spans(events),unknown)
    }

    fn database_segment_events(&mut self, node: &RadValue) -> (Vec<(f64,f64)>,bool) {
        let location=&node["location"];
        // The legacy route matcher pairs first/last named occurrences. Do not
        // join separate traversals when an endpoint repeats in the trajectory.
        for endpoint in [location["parts"][0][0].as_str(),location["parts"][2][0].as_str()] {
            let Some(id)=endpoint else {return (vec![],true);};
            if self.points.iter().filter(|p|p.name==id).count()>1 {return (vec![],true);}
        }
        let (events,mut unknown)=self.events(location);
        unknown |= !self.route_complete();
        let Some(checks)=node["checks"].as_array().filter(|v|!v.is_empty()) else {return (vec![],true);};
        let mut accepted=vec![];
        for (start,end) in events {
            let mut times=vec![start];
            match rad_str(node,"sample") {
                "entry"=>{},
                "endpoints"=>times.push(end),
                "profile"=>{
                    times.push(end);
                    times.extend(self.points.iter().filter_map(|p|p.minute_offset).map(f64::from).filter(|t|*t>start && *t<end));
                },
                _=>return (vec![],true),
            }
            let truth=times.into_iter().fold(RadTruth::Yes,|result,t|{
                let at=self.altitude_at(t,node["requested"]==true).map(|h|
                    checks.iter().fold(RadTruth::Yes,|r,check|r.and(rad_level_compare(h,check))))
                    .unwrap_or(RadTruth::Unknown);
                result.and(at)
            });
            if truth==RadTruth::Yes {accepted.push((start,end));}
            unknown |= truth==RadTruth::Unknown;
        }
        (accepted,unknown)
    }

    fn database_airport_in_airspace(&self, node: &RadValue) -> RadTruth {
        let Some(plan) = self.pilot.flight_plan.as_ref() else { return RadTruth::Unknown; };
        let airport_id = match rad_str(node, "terminal") {
            "dep" => plan.departure_id.as_deref(),
            "arr" => plan.arrival_id.as_deref(),
            _ => return RadTruth::Unknown,
        };
        let Some(airport) = airport_id.and_then(|id| self.reference.navdata.airports.get(id))
            else { return RadTruth::Unknown; };
        let point = geo::Point::new(airport.position.lon, airport.position.lat);
        let ids = rad_values(node);
        if ids.is_empty() { return RadTruth::Unknown; }
        ids.iter().fold(RadTruth::No, |result, id| {
            let mut matches = self.reference.sectors.iter().filter(|s| s.id.as_deref() == Some(*id));
            let Some(sector) = matches.next() else { return result.or(RadTruth::Unknown); };
            if matches.next().is_some() || sector.blocks.is_empty() {
                return result.or(RadTruth::Unknown);
            }
            let inside = sector.blocks.iter().fold(RadTruth::No, |truth, block| {
                if block.polygon.len() < 3 || block.polygon.iter().any(|p|
                    !p.lat.is_finite() || !p.lon.is_finite()
                    || p.lat.abs() > 90. || p.lon.abs() > 180.) {
                    return truth.or(RadTruth::Unknown);
                }
                let ring = Polygon::new(LineString::new(block.polygon.iter()
                    .map(|p| Coord { x:p.lon, y:p.lat }).collect()), vec![]);
                let membership = if ring.contains(&point) { RadTruth::Yes }
                    else if geo::Intersects::intersects(&ring, &point) { RadTruth::Unknown }
                    else { RadTruth::No };
                truth.or(membership)
            });
            result.or(inside)
        })
    }

    fn database_attribute(&self, node: &RadValue) -> RadTruth {
        match rad_str(node, "op") {
            "aircraftClassification" => {
                if !rad_support_valid(&self.support,self.at) { return RadTruth::Unknown; }
                let Some(id)=self.pilot.flight_plan.as_ref().and_then(|p|p.aircraft_id.as_deref())
                    else { return RadTruth::Unknown; };
                rad_native_classification(self.support["aircraft"][id]["classification"].as_str(), &rad_values(node))
            }
            "aircraftOperator" => {
                // A callsign prefix is not proof of the operating agency.
                let flight=self.support_flight();
                let Some(id)=flight["operatorId"].as_str().filter(|s|!s.is_empty())
                    else { return RadTruth::Unknown; };
                let wanted=rad_values(node);
                if wanted.is_empty() { RadTruth::Unknown } else {rad_bool(wanted.contains(&id))}
            }
            _ => RadTruth::Unknown,
        }
    }
    fn database_schedule(&mut self, rule: &RadValue) -> RadTruth {
        let schedule = if rad_str(&rule["schedule"], "op") == "timeAt" {
            &rule["schedule"]["schedule"]
        } else { &rule["schedule"] };
        self.eval(&rad_json!({"op":"timeAt", "location":rule["referenceLocation"],
            "schedule":{"op":"and","args":[schedule,
                {"op":"utcInterval","start":rule["sourceValidity"]["start"],
                 "end":rule["sourceValidity"]["end"]}]}}))
    }
}

#[cfg(test)]
mod rad_native_validity_tests {
    use super::*;
    #[test]
    fn classification_masks_preserve_coupled_engines_and_missing_data() {
        assert_eq!(rad_native_classification(Some("L4J"),&["?4?"]),RadTruth::Yes);
        assert_eq!(rad_native_classification(Some("L2J"),&["?4?"]),RadTruth::No);
        assert_eq!(rad_native_classification(Some("LCP"),&["?C?"]),RadTruth::Yes);
        assert_eq!(rad_native_classification(Some("LCP"),&["?2?"]),RadTruth::No);
        assert_eq!(rad_native_classification(None,&["?4?"]).not(),RadTruth::Unknown);
        assert_eq!(rad_native_classification(Some("unknown"),&["???"]),RadTruth::Unknown);
    }
    #[test]
    fn midnight_keeps_both_candidate_states_and_excludes_end_instant() {
        let midnight: DateTime<Utc> = "2026-11-06T00:00:00Z".parse().unwrap();
        let ms = midnight.timestamp_millis();
        let before = rad_json!({"sourceValidity":{"start":ms-86400000,"end":ms}});
        let after = rad_json!({"sourceValidity":{"start":ms,"end":ms+86400000}});
        let departure = midnight-ChronoDuration::minutes(30);
        let arrival = Some(midnight+ChronoDuration::minutes(30));
        assert!(rad_database_state_overlaps(&before, departure, arrival));
        assert!(rad_database_state_overlaps(&after, departure, arrival));
        assert_eq!(rad_database_interval(midnight,&before["sourceValidity"]),RadTruth::No);
        assert_eq!(rad_database_interval(midnight,&after["sourceValidity"]),RadTruth::Yes);
        assert_eq!(rad_database_interval(midnight,&rad_json!({})),RadTruth::Unknown);
    }
    #[test]
    fn reference_schedule_does_not_require_a_crossing_scope() {
        assert!(rad_schedule_has_own_reference(&rad_json!({"op":"timeAt","location":{"op":"arr","values":["LFPG"]}})));
        assert!(!rad_schedule_has_own_reference(&rad_json!({"op":"time","start":60,"end":120})));
        assert!(!rad_schedule_has_own_reference(&rad_json!({"op":"and","args":[]})));
    }
}
