static RAD_NATIVE_TEST_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

#[test]
#[ignore = "Explicit smoke check of the installed NM catalogue"]
fn rad_native_installed_catalogue_loads_with_diagnostics() {
    let _guard=RAD_NATIVE_TEST_LOCK.lock().unwrap();
    let (mut state,dir,_)=flight_tools_fixture();
    state.data_dir=std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../data");
    let cat=state.rad_catalogue().unwrap();
    assert_eq!(cat.source_kind,"nmp-database");assert!(cat.allow_incomplete_native);
    assert_eq!(cat.rules.len(),cat.summary["rules"].as_u64().unwrap() as usize);
    assert_eq!(cat.version,"NMP-2611-diagnostics");assert!(!cat.operationally_validated);
    println!("Loaded {} native states; {} retain conversion diagnostics",cat.rules.len(),cat.summary["rulesWithPendingSemantics"]);
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn rad_native_enroute_dct_checks_full_length_bands_exceptions_and_short_windows() {
    let _guard=RAD_NATIVE_TEST_LOCK.lock().unwrap();
    let (state,dir,input)=flight_tools_fixture();let mut reference=(*state.reference_data_snapshot()).clone();
    reference.sectors=serde_json::from_value(rad_json!([{"id":"AREA","blocks":[{"floorFt":0,"ceilingFt":40000,
        "polygon":[{"lat":0.,"lon":0.},{"lat":0.,"lon":2.},{"lat":1.,"lon":2.},{"lat":1.,"lon":0.}]}]}])).unwrap();
    let mut pilot=manual_fpl_pilot(serde_json::from_value(input).unwrap(),&reference.navdata).unwrap();
    pilot.flight_plan.as_mut().unwrap().route=Some("AAA DCT BBB".into());
    let mut points=[rad_test_point(0.5,20000.,0),rad_test_point(1.5,20000.,20)];points[0].name="AAA".into();points[1].name="BBB".into();
    let areas=SpecialAreaCache::default();let mut ctx=rad_context_for_test(&pilot,&points,&reference,&areas);let start=ctx.takeoff.timestamp_millis();
    let mut node=rad_json!({"op":"nativeDctViolation","airspace":"AREA","mode":"MANDATORY","limitNm":50,
        "crossingBand":{"minFt":19500,"maxFt":20000},"segmentOptions":[],"schedule":{"op":"true"},
        "sourceValidity":{"start":start,"end":start+86400000}});
    assert_eq!(ctx.eval(&node),RadTruth::Yes);
    node["limitNm"]=70.into();assert_eq!(ctx.eval(&node),RadTruth::No);
    node["limitNm"]=50.into();node["crossingBand"]["maxFt"]=19999.into();assert_eq!(ctx.eval(&node),RadTruth::No);
    node["crossingBand"]["maxFt"]=20000.into();
    node["segmentOptions"]=rad_json!([{"from":"AAA","to":"BBB","minFt":20000,"maxFt":20000}]);
    assert_eq!(ctx.eval(&node),RadTruth::No);
    node["mode"]="FORBIDDEN".into();assert_eq!(ctx.eval(&node),RadTruth::Yes);
    node["segmentOptions"][0]["from"]="BBB".into();node["segmentOptions"][0]["to"]="AAA".into();assert_eq!(ctx.eval(&node),RadTruth::No);
    node["segmentOptions"]=rad_json!([]);node["mode"]="MANDATORY".into();
    node["schedule"]=rad_json!({"op":"utcInterval","start":start+437250,"end":start+440250});
    assert_eq!(ctx.eval(&node),RadTruth::Yes,"short opening inside traversal must be sampled");
    node["sourceValidity"]["end"]=(start+430000).into();assert_eq!(ctx.eval(&node),RadTruth::No);
    node["airspace"]="MISSING".into();assert_eq!(ctx.eval(&node),RadTruth::Unknown);
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn rad_native_fra_border_correlates_direct_leg_role_time_and_direction() {
    let _guard=RAD_NATIVE_TEST_LOCK.lock().unwrap();
    let (state,dir,input)=flight_tools_fixture();let mut reference=(*state.reference_data_snapshot()).clone();
    let sector=|id:&str,left:f64,right:f64|rad_json!({"id":id,"blocks":[{"floorFt":10000,"ceilingFt":40000,
        "polygon":[{"lat":0.,"lon":left},{"lat":0.,"lon":right},{"lat":1.,"lon":right},{"lat":1.,"lon":left}]}]});
    reference.sectors=serde_json::from_value(rad_json!([sector("A",0.,1.),sector("B",1.,2.)])).unwrap();
    let mut pilot=manual_fpl_pilot(serde_json::from_value(input).unwrap(),&reference.navdata).unwrap();
    pilot.flight_plan.as_mut().unwrap().route=Some("MATIK DCT AAA DCT BBB".into());
    let mut points=[rad_test_point(-0.5,20000.,0),rad_test_point(0.5,20000.,10),rad_test_point(1.5,20000.,20)];
    for (p,id) in points.iter_mut().zip(["MATIK","AAA","BBB"]) {p.name=id.into();}
    let areas=SpecialAreaCache::default();
    let mut ctx=rad_context_for_test(&pilot,&points,&reference,&areas);
    let start=ctx.takeoff.timestamp_millis();
    let mut node=rad_json!({"op":"fraBorderViolation","borders":[{"from":"A","to":"B"}],
        "entryExitPoints":["MATIK"],"allowedDctSegments":[],"schedule":{"op":"true"},
        "sourceValidity":{"start":start,"end":start+86400000}});
    assert_eq!(ctx.eval(&node),RadTruth::Yes,"MATIK elsewhere must not permit AAA..BBB");
    node["crossingBand"]=rad_json!({"minFt":20000,"maxFt":20000});
    assert_eq!(ctx.eval(&node),RadTruth::Yes,"both crossing bounds are inclusive");
    node["crossingBand"]["minFt"]=20001.into();
    assert_eq!(ctx.eval(&node),RadTruth::No);
    node.as_object_mut().unwrap().remove("crossingBand");
    node["pointOptions"]=rad_json!([{"point":"AAA","minFt":20000,"maxFt":20000}]);
    assert_eq!(ctx.eval(&node),RadTruth::No,"point permission includes its exact level");
    node["pointOptions"][0]["minFt"]=20001.into();assert_eq!(ctx.eval(&node),RadTruth::Yes);
    node["pointOptions"]=rad_json!([]);
    node["segmentOptions"]=rad_json!([{"from":"AAA","to":"BBB","minFt":20000,"maxFt":20000}]);
    assert_eq!(ctx.eval(&node),RadTruth::No);
    node["segmentOptions"][0]["maxFt"]=19999.into();assert_eq!(ctx.eval(&node),RadTruth::Yes);
    node["segmentOptions"]=rad_json!([]);
    node["entryExitPoints"]=rad_json!(["AAA"]);
    assert_eq!(ctx.eval(&node),RadTruth::No,"authorized endpoint need not be exactly on the border");
    node["entryExitPoints"]=rad_json!([]);node["allowedDctSegments"]=rad_json!([["AAA","BBB"]]);
    assert_eq!(ctx.eval(&node),RadTruth::No);
    node["allowedDctSegments"]=rad_json!([["BBB","AAA"]]);assert_eq!(ctx.eval(&node),RadTruth::Yes);
    node["schedule"]=rad_json!({"op":"utcInterval","start":start,"end":start+600000});
    assert_eq!(ctx.eval(&node),RadTruth::No,"inactive at crossing");
    node["schedule"]=rad_json!({"op":"true"});node["borders"]=rad_json!([{"from":"B","to":"A"}]);
    assert_eq!(ctx.eval(&node),RadTruth::No,"opposite direction does not match");
    node["borders"]=rad_json!([{"from":"A","to":"MISSING"}]);assert_eq!(ctx.eval(&node),RadTruth::Unknown);
    node["borders"]=rad_json!([{"from":"A","to":"B"},{"from":"B","to":"A"}]);
    node["allowedDctSegments"]=rad_json!([]);node["entryExitPoints"]=rad_json!(["AAA"]);
    let mut repeated=Vec::from(points);let mut last=rad_test_point(0.5,20000.,30);last.name="CCC".into();repeated.push(last);
    pilot.flight_plan.as_mut().unwrap().route=Some("MATIK DCT AAA DCT BBB DCT CCC".into());
    let mut ctx=rad_context_for_test(&pilot,&repeated,&reference,&areas);
    assert_eq!(ctx.eval(&node),RadTruth::Yes,"the second crossing is not allowed by the first one's endpoint");
    node["schedule"]=rad_json!({"op":"utcInterval","start":start,"end":start+1200000});
    assert_eq!(ctx.eval(&node),RadTruth::No,"only the permitted first crossing is active");
    let rule=rad_json!({"referenceLocation":{"op":"fraBorder","borders":node["borders"]},
        "schedule":node["schedule"],"sourceValidity":node["sourceValidity"]});
    assert_eq!(ctx.database_schedule(&rule),RadTruth::Yes);
    node["schedule"]=rad_json!({"op":"true"});node["entryExitPoints"]=rad_json!([]);
    node["sourceValidity"]["end"]=(start+900000).into();
    assert_eq!(ctx.eval(&node),RadTruth::No,"validity end is exclusive at the crossing");
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn rad_native_mandatory_volume_anchors_one_passage_and_respects_rfl() {
    let _guard=RAD_NATIVE_TEST_LOCK.lock().unwrap();
    let (state,dir,input)=flight_tools_fixture();let mut reference=(*state.reference_data_snapshot()).clone();
    reference.sectors=serde_json::from_value(rad_json!([{"id":"TEST","blocks":[{"floorFt":10000,"ceilingFt":30000,
        "polygon":[{"lat":0.,"lon":0.},{"lat":0.,"lon":2.},{"lat":1.,"lon":2.},{"lat":1.,"lon":0.}]}]}])).unwrap();
    let pilot=manual_fpl_pilot(serde_json::from_value(input).unwrap(),&reference.navdata).unwrap();
    let mut points=[rad_test_point(-0.5,5000.,0),rad_test_point(2.5,5000.,60)];
    points[0].distance_nm=Some(0.);points[1].distance_nm=Some(180.);
    let areas=SpecialAreaCache::default();let mut ctx=rad_context_for_test(&pilot,&points,&reference,&areas);
    let mut cap=rad_json!({"airspace":"TEST","mandatoryAirspaceBand":{"min":"FLOOR","max":"CEILING"}});
    let caps=ctx.database_profile_adjustment(&cap).unwrap();assert_eq!(caps.len(),1);
    let (a,b)=caps[0].distance_span.unwrap();assert!((a-90.).abs()<1e-5);assert_eq!(a,b);
    assert_eq!(caps[0].min_ft,Some(10000.));assert_eq!(caps[0].max_ft,Some(30000.));
    cap["mandatoryAirspaceBand"]["min"]=50000.into();cap["mandatoryAirspaceBand"]["max"]=60000.into();
    assert!(ctx.database_profile_adjustment(&cap).is_none());
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn rad_native_ptr_choices_preserve_or_and_forbidden_union() {
    let _guard=RAD_NATIVE_TEST_LOCK.lock().unwrap();
    let (state,dir,input)=flight_tools_fixture();let reference=state.reference_data_snapshot();
    let pilot=manual_fpl_pilot(serde_json::from_value(input).unwrap(),&reference.navdata).unwrap();
    let mut points=[rad_test_point(0.,20000.,0),rad_test_point(1.,20000.,60)];points[0].name="AAA".into();points[1].name="BBB".into();
    let areas=SpecialAreaCache::default();let mut ctx=rad_context_for_test(&pilot,&points,&reference,&areas);
    let choices=["AAA","BBB"].into_iter().map(|id|rad_json!({"scope":{"op":"via","values":[id]},
        "schedule":{"op":"true"},"sourceValidity":{"start":ctx.takeoff.timestamp_millis()-1,"end":ctx.takeoff.timestamp_millis()+86400000},
        "referenceLocation":{"op":"via","values":[id]},"adjustment":{"point":id,"minFt":null,"maxFt":18000}})).collect::<Vec<_>>();
    let mut group=rad_json!({"mode":"MANDATORY","profileChoices":choices});
    assert_eq!(ctx.database_profile_adjustment(&group).unwrap().len(),1);
    group["mode"]="FORBIDDEN".into();assert_eq!(ctx.database_profile_adjustment(&group).unwrap().len(),2);
    group["profileChoices"][0]["scope"]=rad_json!({"op":"unknown"});
    assert!(ctx.database_profile_adjustment(&group).is_none());
    group["mode"]="MANDATORY".into();assert_eq!(ctx.database_profile_adjustment(&group).unwrap()[0].point,"BBB");
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn rad_native_route_ptr_and_airspace_extensions_generate_continuous_constraints() {
    let _guard=RAD_NATIVE_TEST_LOCK.lock().unwrap();
    let (state,dir,input)=flight_tools_fixture();let mut reference=(*state.reference_data_snapshot()).clone();
    let mut nav=(*reference.navdata).clone();
    for (id,lon) in [("AAA",-0.5),("BBB",0.5),("CCC",2.5)] {nav.points.insert(id.into(),Point{lat:0.5,lon});}
    nav.airways.insert("R1".into(),vec!["AAA".into(),"BBB".into(),"CCC".into()]);
    nav.airports.get_mut("LFBO").unwrap().position=Point{lat:0.5,lon:2.5};reference.navdata=Arc::new(nav);
    reference.sectors=serde_json::from_value(rad_json!([{"id":"TEST","blocks":[{"floorFt":10000,"ceilingFt":30000,
        "polygon":[{"lat":0.,"lon":0.},{"lat":0.,"lon":2.},{"lat":1.,"lon":2.},{"lat":1.,"lon":0.}]}]}])).unwrap();
    let mut pilot=manual_fpl_pilot(serde_json::from_value(input).unwrap(),&reference.navdata).unwrap();
    pilot.flight_plan.as_mut().unwrap().route=Some("BBB R1 CCC".into());
    let mut points=[rad_test_point(-0.5,20000.,0),rad_test_point(0.5,20000.,10),rad_test_point(2.5,20000.,30)];
    for ((p,id),d) in points.iter_mut().zip(["AAA","BBB","CCC"]).zip([0.,50.,150.]) {p.name=id.into();p.distance_nm=Some(d);}
    let areas=SpecialAreaCache::default();let mut ctx=rad_context_for_test(&pilot,&points,&reference,&areas);
    let mut cap=rad_json!({"routeLocation":{"op":"routePointList","parts":[["AAA"],["R1"],["CCC"]],"partial":true,"requireFiledRoute":true},
        "mode":"MANDATORY","minFt":15000,"maxFt":25000});
    let caps=ctx.database_route_profile_constraints(&cap).unwrap();assert_eq!(caps.len(),1);
    assert_eq!(caps[0].distance_span,Some((50.,150.)));assert_eq!(caps[0].min_ft,Some(15000.));
    cap["minFt"]=40000.into();cap["maxFt"]=45000.into();assert!(ctx.database_route_profile_constraints(&cap).is_none());
    points[0].name="LFPG".into();points[2].name="LFBO".into();
    let mut ctx=rad_context_for_test(&pilot,&points,&reference,&areas);
    let cap=rad_json!({"airspace":"TEST","forbiddenAirspaceBand":{"min":10000,"max":"UNLIMITED"},
        "extensionTerminals":[{"terminal":"arr","condition":{"op":"arr","values":["LFBO"]}}]});
    let caps=ctx.database_airspace_profile_constraints(&cap).unwrap();assert_eq!(caps.len(),1);
    assert!((caps[0].distance_span.unwrap().0-25.).abs()<1e-6);assert_eq!(caps[0].distance_span.unwrap().1,150.);
    assert_eq!(caps[0].max_ft,Some(10000.));
    let d=rad_native_arc_distance_nm(Point{lat:1.,lon:0.},Point{lat:0.,lon:-1.},Point{lat:0.,lon:1.}).unwrap();
    assert!((d-60.04).abs()<0.02);
    assert!(rad_native_arc_distance_nm(Point{lat:1.,lon:0.},Point{lat:0.,lon:0.},Point{lat:0.,lon:180.}).is_none());
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn rad_native_route_availability_requires_an_open_level_in_every_segment() {
    let _guard=RAD_NATIVE_TEST_LOCK.lock().unwrap();
    let (state,dir,input)=flight_tools_fixture();let reference=state.reference_data_snapshot();
    let pilot=manual_fpl_pilot(serde_json::from_value(input).unwrap(),&reference.navdata).unwrap();
    let points=[rad_test_point(0.,15000.,0),rad_test_point(1.,15000.,60)];let areas=SpecialAreaCache::default();
    let mut ctx=rad_context_for_test(&pilot,&points,&reference,&areas);
    let a=rad_json!({"backendId":"A","uuid":"a"});let b=rad_json!({"backendId":"B","uuid":"b"});let c=rad_json!({"backendId":"C","uuid":"c"});
    let node=rad_json!({"op":"nativeRouteAvailability","contextKey":"R|A|C","sourceRouteRef":{"backendId":"R","uuid":"r"},
        "fromPointRef":a,"toPointRef":c,"minFt":19500,"maxFt":24500,"offsetStartMinutes":0,"offsetEndMinutes":0});
    assert_eq!(ctx.eval(&node),RadTruth::Unknown);
    let window=rad_json!({"validFrom":"2026-11-05T12:00:00Z","validUntil":"2026-11-05T13:00:00Z","minFt":20000,"maxFt":21000});
    let mut support=rad_json!({"validFrom":"2026-11-05T00:00:00Z","validUntil":"2026-11-06T00:00:00Z",
        "nativeRouteAvailability":{"R|A|C":{"source":"NM test","complete":true,"sourceRouteRef":node["sourceRouteRef"],
        "fromPointRef":a,"toPointRef":c,"validFrom":"2026-11-05T00:00:00Z","validUntil":"2026-11-06T00:00:00Z",
        "segments":[{"fromPointRef":a,"toPointRef":b,"windows":[window.clone()]},
            {"fromPointRef":b,"toPointRef":c,"windows":[window]}]}}});
    ctx.support=Arc::new(support.clone());assert_eq!(ctx.eval(&node),RadTruth::Yes);
    support["nativeRouteAvailability"]["R|A|C"]["segments"][1]["windows"][0]["minFt"]=23000.into();
    support["nativeRouteAvailability"]["R|A|C"]["segments"][1]["windows"][0]["maxFt"]=24000.into();
    ctx.support=Arc::new(support.clone());assert_eq!(ctx.eval(&node),RadTruth::Yes);
    support["nativeRouteAvailability"]["R|A|C"]["segments"][1]["windows"]=rad_json!([]);
    ctx.support=Arc::new(support.clone());assert_eq!(ctx.eval(&node),RadTruth::No);
    support["nativeRouteAvailability"]["R|A|C"]["complete"]=false.into();ctx.support=Arc::new(support);
    assert_eq!(ctx.eval(&rad_json!({"op":"not","args":[node]})),RadTruth::Unknown);
    let start=ctx.takeoff.timestamp_millis()+17250;
    assert!(ctx.database_schedule_samples(0.,1.,&rad_json!({"op":"utcInterval","start":start,"end":start+3000})).contains(&18));
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn rad_native_airspace_ptr_resolves_each_volume_and_keeps_missing_bounds_unknown() {
    let _guard=RAD_NATIVE_TEST_LOCK.lock().unwrap();
    let (state,dir,input)=flight_tools_fixture();
    let mut reference=(*state.reference_data_snapshot()).clone();
    reference.sectors=serde_json::from_value(rad_json!([{"id":"TEST","blocks":[{
        "floorFt":10000,"ceilingFt":30000,"polygon":[{"lat":0.,"lon":0.},{"lat":0.,"lon":2.},
        {"lat":1.,"lon":2.},{"lat":1.,"lon":0.}]}]}])).unwrap();
    let pilot=manual_fpl_pilot(serde_json::from_value(input).unwrap(),&reference.navdata).unwrap();
    let areas=SpecialAreaCache::default();
    let cap=rad_json!({"airspace":"TEST","forbiddenAirspaceBand":{"min":"FLOOR","max":"CEILING"}});
    for (height,min,max) in [(20000.,None,Some(10000.)),(32000.,Some(30000.),None)] {
        let mut points=[rad_test_point(-0.5,height,0),rad_test_point(0.5,height,10),rad_test_point(2.5,height,30)];
        for (p,d) in points.iter_mut().zip([0.,50.,150.]) {p.distance_nm=Some(d);}
        let mut ctx=rad_context_for_test(&pilot,&points,&reference,&areas);
        let constraints=ctx.database_airspace_profile_constraints(&cap).unwrap();
        assert_eq!(constraints.len(),1);assert_eq!(constraints[0].min_ft,min);assert_eq!(constraints[0].max_ft,max);
        let (a,b)=constraints[0].distance_span.unwrap();assert!((a-25.).abs()<1e-6);assert!((b-125.).abs()<1e-6);
    }
    reference.sectors[0].blocks[0].floor_ft=None;
    let points=[rad_test_point(0.2,20000.,0),rad_test_point(1.5,20000.,20)];
    let mut ctx=rad_context_for_test(&pilot,&points,&reference,&areas);
    assert!(ctx.database_airspace_profile_constraints(&cap).is_none());
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn rad_native_closed_for_cruising_detects_level_portions_without_blocking_climbs() {
    let _guard=RAD_NATIVE_TEST_LOCK.lock().unwrap();
    let (state,dir,input)=flight_tools_fixture();
    let mut reference=(*state.reference_data_snapshot()).clone();
    reference.sectors=serde_json::from_value(rad_json!([{"id":"TEST","blocks":[{
        "floorFt":10000,"ceilingFt":40000,"polygon":[{"lat":0.,"lon":0.},{"lat":0.,"lon":2.},
        {"lat":1.,"lon":2.},{"lat":1.,"lon":0.}]}]}])).unwrap();
    let pilot=manual_fpl_pilot(serde_json::from_value(input).unwrap(),&reference.navdata).unwrap();
    let areas=SpecialAreaCache::default();
    let node=rad_json!({"op":"cruiseAt","location":{"op":"via","values":["TEST"]}});
    for (heights,expected) in [([20000.,20000.,20000.],RadTruth::Yes),
        ([20000.,22000.,24000.],RadTruth::No),([24000.,22000.,20000.],RadTruth::No),
        ([20000.,22000.,22000.],RadTruth::Yes),([20000.,20000.01,20000.02],RadTruth::No)] {
        let points=heights.into_iter().enumerate().map(|(i,h)|rad_test_point(0.2+i as f64*0.7,h,i as u32*10)).collect::<Vec<_>>();
        let mut ctx=rad_context_for_test(&pilot,&points,&reference,&areas);
        assert_eq!(ctx.eval(&node),expected);
    }
    let mut points=[rad_test_point(0.2,20000.,0),rad_test_point(1.5,20000.,20)];
    points[1].altitude_ft=None;
    let mut ctx=rad_context_for_test(&pilot,&points,&reference,&areas);
    assert_eq!(ctx.eval(&node),RadTruth::Unknown);
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn rad_native_procedure_reference_uses_resolved_sid_star_first_segments() {
    let _guard=RAD_NATIVE_TEST_LOCK.lock().unwrap();
    let (state,dir,input)=flight_tools_fixture();
    let mut reference=(*state.reference_data_snapshot()).clone();
    let mut nav=(*reference.navdata).clone();
    for (i,id) in ["AAA","BBB","CCC"].iter().enumerate() {nav.points.insert((*id).into(),Point{lat:0.5,lon:i as f64});}
    nav.procedures.insert("LFPG".into(),serde_json::from_value(rad_json!([{"airport":"LFPG","procedureType":"SID","name":"TEST1A",
        "legs":[{"fix":"AAA"},{"fix":"BBB"},{"fix":"CCC"}]}])).unwrap());
    nav.procedures.insert("LFBO".into(),serde_json::from_value(rad_json!([{"airport":"LFBO","procedureType":"STAR","name":"TEST2A",
        "legs":[{"fix":"AAA"},{"fix":"BBB"},{"fix":"CCC"}]}])).unwrap());
    reference.navdata=Arc::new(nav.clone());
    let mut pilot=manual_fpl_pilot(serde_json::from_value(input).unwrap(),&reference.navdata).unwrap();
    let mut points=(0..5).map(|i|rad_test_point(i as f64,20000.,i*10)).collect::<Vec<_>>();
    for (p,id) in points.iter_mut().zip(["LFPG","AAA","BBB","CCC","LFBO"]) {p.name=id.into();}
    let areas=SpecialAreaCache::default();
    pilot.flight_plan.as_mut().unwrap().route=Some("TEST1A CCC DCT".into());
    let mut ctx=rad_context_for_test(&pilot,&points,&reference,&areas);
    let sid=rad_json!({"op":"procedureSpan","values":["TEST1A"]});
    assert_eq!(ctx.events(&sid),(vec![(0.,30.)],false));
    assert_eq!(ctx.events(&rad_json!({"op":"firstSegment","location":sid})),(vec![(0.,10.)],false));
    pilot.flight_plan.as_mut().unwrap().route=Some("DCT AAA TEST2A".into());
    let mut ctx=rad_context_for_test(&pilot,&points,&reference,&areas);
    let star=rad_json!({"op":"procedureSpan","values":["TEST2A"]});
    assert_eq!(ctx.events(&rad_json!({"op":"firstSegment","location":star.clone()})),(vec![(10.,20.)],false));
    assert_eq!(ctx.eval(&sid),RadTruth::No);
    nav.procedures.get_mut("LFBO").unwrap()[0].legs[0].fix=None;
    reference.navdata=Arc::new(nav);
    let mut ctx=rad_context_for_test(&pilot,&points,&reference,&areas);
    assert_eq!(ctx.eval(&star),RadTruth::Unknown);
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn rad_native_point_ptr_changes_profile_without_changing_filed_level_or_rejecting_fpl() {
    let _guard=RAD_NATIVE_TEST_LOCK.lock().unwrap();
    let (state,dir,mut input)=flight_tools_fixture();
    let mut reference=(*state.reference_data_snapshot()).clone();
    let mut nav=(*reference.navdata).clone();
    nav.points.insert("MID".into(),Point{lat:46.3,lon:1.7});
    reference.route_navdata=Arc::new(build_route_navdata(&nav));reference.navdata=Arc::new(nav);
    *state.reference_data.write().unwrap()=Arc::new(reference.clone());
    input["route"]="DCT MID DCT".into();
    let pilot=manual_fpl_pilot(serde_json::from_value(input.clone()).unwrap(),&reference.navdata).unwrap();
    let now=Utc::now();let takeoff=state.native_profile_takeoff(&pilot,now);
    let points=iatfm_core::dashboard::build_pilot_vertical_profile(now,&pilot,&[],&reference.route_navdata,
        &reference.navdata,&BTreeMap::new(),&state.aircraft_performance,None);
    let old_height=points.iter().find(|p|p.name=="MID").unwrap().altitude_ft.unwrap();
    assert!(old_height>18000.);
    let rule=rad_json!({"id":"PTR_TEST","key":"PTR_TEST:1","sheet":"NMP:PTR","effect":"profileAdjustment",
        "validFrom":"2026-10-29","validUntil":"2026-11-26","conversionDiagnostics":[],
        "sourceValidity":{"start":takeoff.timestamp_millis()-86400000,"end":takeoff.timestamp_millis()+86400000},
        "scope":{"op":"level","comparison":"ABV","value":20000,"requested":true,"location":{"op":"via","values":["MID"]}},
        "schedule":{"op":"true"},"referenceLocation":{"op":"via","values":["MID"]},
        "clauses":[{"action":"profileAdjustment","condition":{"op":"true"}}],
        "profileAdjustments":[{"point":"MID","minFt":null,"maxFt":18000}]});
    install_rad_test_catalogue(&state,&dir,rad_json!([rule.clone()]));
    let (constraints,metadata)=state.native_profile_constraints(&pilot,&points,&reference,takeoff);
    assert_eq!(constraints.len(),1,"{metadata}");
    let adjusted=iatfm_core::dashboard::build_native_constrained_profile(now,&pilot,&[],&reference.route_navdata,
        &reference.navdata,&BTreeMap::new(),&state.aircraft_performance,None,&constraints).unwrap();
    assert!(adjusted.iter().find(|p|p.name=="MID").unwrap().altitude_ft.unwrap()<=18000.01);
    assert_eq!(pilot.flight_plan.as_ref().unwrap().level.as_deref(),Some("F350"));
    let result=checked_fpl(&state,&flight_check_request(&input));
    assert_eq!(result["rad"]["profileTuning"]["profileAdjusted"],true,"{result}");
    assert!(!result["validation"]["checks"].as_array().unwrap().iter().any(|v|v["ruleId"]=="PTR_TEST" && v["status"]=="error"));
    let contradictory=[iatfm_core::dashboard::NativeProfileConstraint{point:"MID".into(),min_ft:Some(20000.),max_ft:Some(18000.),through:None,distance_span:None}];
    assert!(iatfm_core::dashboard::build_native_constrained_profile(now,&pilot,&[],&reference.route_navdata,
        &reference.navdata,&BTreeMap::new(),&state.aircraft_performance,None,&contradictory).is_err());
    // Extension is conditional on the actual terminal and the 200-km radius.
    let mut extended=rule.clone();
    extended["profileAdjustments"][0]["extensionTerminals"]=rad_json!([
        {"terminal":"arr","condition":{"op":"arr","values":["LFBO"]}}]);
    let mut nearby=(*reference.navdata).clone();
    nearby.airports.get_mut("LFBO").unwrap().position=nearby.points["MID"];
    let mut near_reference=reference.clone();near_reference.navdata=Arc::new(nearby);
    install_rad_test_catalogue(&state,&dir,rad_json!([extended.clone()]));
    let (caps,metadata)=state.native_profile_constraints(&pilot,&points,&near_reference,takeoff);
    assert!(caps.iter().any(|c|c.through.as_deref()==Some("LFBO")),"{metadata}");
    let (caps,_)=state.native_profile_constraints(&pilot,&points,&reference,takeoff);
    assert!(!caps.iter().any(|c|c.through.is_some()));
    extended["profileAdjustments"][0]["extensionTerminals"][0]["condition"]["values"]=rad_json!(["LFPG"]);
    install_rad_test_catalogue(&state,&dir,rad_json!([extended]));
    let (caps,_)=state.native_profile_constraints(&pilot,&points,&near_reference,takeoff);
    assert!(!caps.iter().any(|c|c.through.is_some()));
    // A peak between the constrained point and airport is checked as well.
    let extension=[iatfm_core::dashboard::NativeProfileConstraint {
        point:"MID".into(),min_ft:None,max_ft:Some(18000.),through:Some("LFPG".into()),distance_span:None}];
    if let Ok(profile)=iatfm_core::dashboard::build_native_constrained_profile(now,&pilot,&[],&reference.route_navdata,
        &reference.navdata,&BTreeMap::new(),&state.aircraft_performance,None,&extension) {
        let last=profile.iter().position(|p|p.name=="MID").unwrap();
        assert!(profile[..=last].iter().all(|p|p.altitude_ft.is_some_and(|h|h<=18000.01)));
    }
    let mut band=rule.clone();
    band["profileAdjustments"][0]=rad_json!({"point":"MID","forbiddenBand":{"minFt":20000,"maxFt":30000}});
    install_rad_test_catalogue(&state,&dir,rad_json!([band]));
    for (height,expected_min,expected_max) in [(19000.,None,Some(20000.)),(21000.,None,Some(20000.)),
        (29000.,Some(30000.),Some(35000.)),(34000.,Some(30000.),Some(35000.))] {
        let raw=points.iter().map(|p|iatfm_core::dashboard::VerticalProfilePoint {
            name:p.name.clone(),distance_nm:p.distance_nm,minute_offset:p.minute_offset,position:p.position,
            altitude_ft:if p.name=="MID" {Some(height)}else{p.altitude_ft},
        }).collect::<Vec<_>>();
        let (caps,metadata)=state.native_profile_constraints(&pilot,&raw,&reference,takeoff);
        assert_eq!(caps.len(),1,"{metadata}");assert_eq!(caps[0].min_ft,expected_min);assert_eq!(caps[0].max_ft,expected_max);
    }
    let span=[iatfm_core::dashboard::NativeProfileConstraint {
        point:String::new(),min_ft:None,max_ft:Some(18000.),through:None,distance_span:Some((20.,80.))}];
    let limited=iatfm_core::dashboard::build_native_constrained_profile(now,&pilot,&[],&reference.route_navdata,
        &reference.navdata,&BTreeMap::new(),&state.aircraft_performance,None,&span).unwrap();
    assert!(limited.iter().any(|p|p.name.starts_with("NMP PTR BOUNDARY")));
    assert!(limited.iter().filter(|p|p.distance_nm.is_some_and(|d|(20. ..=80.).contains(&d)))
        .all(|p|p.altitude_ft.is_some_and(|h|h<=18000.01)));
    let anchor=[iatfm_core::dashboard::NativeProfileConstraint {
        point:String::new(),min_ft:Some(15000.),max_ft:Some(18000.),through:None,distance_span:Some((60.,60.))}];
    let anchored=iatfm_core::dashboard::build_native_constrained_profile(now,&pilot,&[],&reference.route_navdata,
        &reference.navdata,&BTreeMap::new(),&state.aircraft_performance,None,&anchor).unwrap();
    assert!(anchored.iter().any(|p|p.distance_nm.is_some_and(|d|(d-60.).abs()<1e-5)
        && p.altitude_ft.is_some_and(|h|(14999.99..=18000.01).contains(&h))),"{anchored:?}");
    let mut above_rfl=rule;
    above_rfl["profileAdjustments"][0]=rad_json!({"point":"MID","minFt":40000,"maxFt":null});
    install_rad_test_catalogue(&state,&dir,rad_json!([above_rfl]));
    let (constraints,metadata)=state.native_profile_constraints(&pilot,&points,&reference,takeoff);
    assert!(constraints.is_empty());
    assert_eq!(metadata["pendingRules"],rad_json!(["PTR_TEST"]));
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn rad_native_relative_airspace_slices_use_each_blocks_vertical_limits() {
    let _guard=RAD_NATIVE_TEST_LOCK.lock().unwrap();
    let (state,dir,input)=flight_tools_fixture();
    let mut reference=(*state.reference_data_snapshot()).clone();
    let pilot=manual_fpl_pilot(serde_json::from_value(input).unwrap(),&reference.navdata).unwrap();
    let polygon=rad_json!([{"lat":0.,"lon":0.},{"lat":0.,"lon":2.},
                          {"lat":1.,"lon":2.},{"lat":1.,"lon":0.}]);
    reference.sectors=serde_json::from_value(rad_json!([{"id":"TEST","blocks":[
        {"floorFt":20000,"ceilingFt":30000,"polygon":polygon},
        {"floorFt":30000,"ceilingFt":40000,"polygon":polygon}]}])).unwrap();
    let areas=SpecialAreaCache::default();
    for (height,low,high,expected) in [
        (15000.,rad_json!("GROUND"),rad_json!("CEILING"),RadTruth::Yes),
        (35000.,rad_json!(19500),rad_json!("CEILING"),RadTruth::Yes),
        (40000.,rad_json!(19500),rad_json!("CEILING"),RadTruth::No),
        (25000.,rad_json!("FLOOR"),rad_json!("UNLIMITED"),RadTruth::Yes),
        (20000.,rad_json!("FLOOR"),rad_json!("UNLIMITED"),RadTruth::No),
        (15000.,rad_json!("FLOOR"),rad_json!(19000),RadTruth::No),
        (15000.,rad_json!("GROUND"),rad_json!("FLOOR"),RadTruth::Yes),
        (25000.,rad_json!("GROUND"),rad_json!("FLOOR"),RadTruth::No),
        (35000.,rad_json!("CEILING"),rad_json!("UNLIMITED"),RadTruth::No),
        (45000.,rad_json!("CEILING"),rad_json!("UNLIMITED"),RadTruth::Yes)] {
        let points=[rad_test_point(0.5,height,0),rad_test_point(1.5,height,20)];
        let mut ctx=rad_context_for_test(&pilot,&points,&reference,&areas);
        assert_eq!(ctx.eval(&rad_json!({"op":"airspaceSlice","values":["TEST"],"min":low,"max":high,"requested":false})),expected);
    }
    reference.sectors[0].blocks[0].floor_ft=None;
    let points=[rad_test_point(0.5,15000.,0),rad_test_point(1.5,15000.,20)];
    let mut ctx=rad_context_for_test(&pilot,&points,&reference,&areas);
    assert_eq!(ctx.eval(&rad_json!({"op":"airspaceSlice","values":["TEST"],"min":"FLOOR","max":19000})),RadTruth::Unknown);
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn rad_native_registration_prefix_preserves_missing_and_conflicting_fields() {
    let _guard=RAD_NATIVE_TEST_LOCK.lock().unwrap();
    let (state,dir,input)=flight_tools_fixture();
    let reference=state.reference_data_snapshot();
    let mut pilot=manual_fpl_pilot(serde_json::from_value(input).unwrap(),&reference.navdata).unwrap();
    let points=[rad_test_point(0.,20000.,0),rad_test_point(1.,20000.,60)];
    let areas=SpecialAreaCache::default();
    let node=rad_json!({"op":"registrationPrefix","values":["RA"]});
    for (registration,remarks,expected) in [
        (Some("ra-12345"),None,RadTruth::Yes),
        (Some("EW12345"),None,RadTruth::No),
        (None,None,RadTruth::Unknown),
        (None,Some("REG/RA12345"),RadTruth::Yes),
        (Some("RA12345"),Some("REG/EW12345"),RadTruth::Unknown)] {
        let plan=pilot.flight_plan.as_mut().unwrap();
        plan.registration=registration.map(str::to_string);plan.remarks=remarks.map(str::to_string);
        let mut ctx=rad_context_for_test(&pilot,&points,&reference,&areas);
        assert_eq!(ctx.eval(&node),expected);
    }
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn rad_native_point_list_matches_all_points_and_rejects_ambiguous_graphs() {
    let _guard=RAD_NATIVE_TEST_LOCK.lock().unwrap();
    let (state,dir,input)=flight_tools_fixture();
    let mut reference=(*state.reference_data_snapshot()).clone();
    let mut nav=(*reference.navdata).clone();
    for (i,id) in ["AAA","BBB","CCC","XXX"].iter().enumerate() {
        nav.points.insert((*id).into(),Point{lat:0.5,lon:i as f64});
    }
    nav.airways.insert("R1".into(),vec!["AAA".into(),"BBB".into(),"CCC".into()]);
    reference.navdata=Arc::new(nav.clone());
    let mut pilot=manual_fpl_pilot(serde_json::from_value(input).unwrap(),&reference.navdata).unwrap();
    pilot.flight_plan.as_mut().unwrap().route=Some("AAA DCT BBB DCT CCC".into());
    let areas=SpecialAreaCache::default();
    let node=rad_json!({"op":"routePointList","parts":[["AAA"],["R1"],["CCC"]]});
    for (ids,expected) in [(vec!["AAA","BBB","CCC"],RadTruth::Yes),
                           (vec!["AAA","CCC"],RadTruth::No),
                           (vec!["AAA","XXX","BBB","CCC"],RadTruth::No),
                           (vec!["CCC","BBB","AAA"],RadTruth::No),
                           (vec!["AAA","TOC","BBB","CCC"],RadTruth::Yes),
                           (vec!["AAA","NMP PTR BOUNDARY 0","BBB","CCC"],RadTruth::Yes)] {
        let mut points=(0..ids.len()).map(|i|rad_test_point(i as f64,25000.,i as u32*10)).collect::<Vec<_>>();
        for (p,id) in points.iter_mut().zip(ids) {p.name=id.into();}
        let mut ctx=rad_context_for_test(&pilot,&points,&reference,&areas);
        assert_eq!(ctx.eval(&node),expected);
    }
    // PTR applies to an intermediate segment; HS still requires the full list.
    let mut partial=node.clone();partial["partial"]=true.into();
    let mut points=[rad_test_point(1.,25000.,10),rad_test_point(2.,25000.,20)];
    points[0].name="BBB".into();points[1].name="CCC".into();
    let mut ctx=rad_context_for_test(&pilot,&points,&reference,&areas);
    assert_eq!(ctx.eval(&node),RadTruth::No);
    assert_eq!(ctx.eval(&partial),RadTruth::Yes);
    partial["requireFiledRoute"]=true.into();
    assert_eq!(ctx.eval(&partial),RadTruth::No);
    pilot.flight_plan.as_mut().unwrap().route=Some("BBB R1 CCC".into());
    let mut ctx=rad_context_for_test(&pilot,&points,&reference,&areas);
    assert_eq!(ctx.eval(&partial),RadTruth::Yes);
    points.reverse();
    let mut ctx=rad_context_for_test(&pilot,&points,&reference,&areas);
    assert_eq!(ctx.eval(&partial),RadTruth::No);
    // Two possible native expansions must stay unknown, even if one is shorter.
    nav.airway_segments.insert("R1".into(),serde_json::from_value(rad_json!([
        {"from":"AAA","to":"BBB"},{"from":"BBB","to":"CCC"},
        {"from":"AAA","to":"CCC"}])).unwrap());
    reference.navdata=Arc::new(nav);
    let mut points=[rad_test_point(0.,25000.,0),rad_test_point(1.,25000.,10)];
    points[0].name="AAA".into();points[1].name="CCC".into();
    let mut ctx=rad_context_for_test(&pilot,&points,&reference,&areas);
    assert_eq!(ctx.eval(&node),RadTruth::Unknown);
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn rad_native_terminal_distance_handles_direct_procedure_and_missing_fix() {
    let _guard=RAD_NATIVE_TEST_LOCK.lock().unwrap();
    let (state,dir,input)=flight_tools_fixture();
    let mut reference=(*state.reference_data_snapshot()).clone();
    let mut pilot=manual_fpl_pilot(serde_json::from_value(input).unwrap(),&reference.navdata).unwrap();
    let departure=pilot.flight_plan.as_ref().unwrap().departure_id.as_ref().unwrap();
    let position=reference.navdata.airports[departure].position;
    let mut nav=(*reference.navdata).clone();
    nav.points.insert("FAR".into(),Point{lat:position.lat+1.,lon:position.lon});
    nav.procedures.insert(departure.clone(),serde_json::from_value(rad_json!([
        {"airport":departure,"procedureType":"SID","name":"TEST1A","legs":[]}])).unwrap());
    reference.navdata=Arc::new(nav);
    pilot.flight_plan.as_mut().unwrap().route=Some("DCT FAR".into());
    let points=[rad_test_point(0.,10000.,0),rad_test_point(1.,10000.,60)];
    let areas=SpecialAreaCache::default();
    let node=rad_json!({"op":"terminalDctLength","terminal":"dep","value":20});
    let mut ctx=rad_context_for_test(&pilot,&points,&reference,&areas);
    assert_eq!(ctx.eval(&node),RadTruth::Yes);
    assert_eq!(ctx.eval(&rad_json!({"op":"terminalDctLength","terminal":"dep","value":100})),RadTruth::No);
    assert_eq!(ctx.eval(&rad_json!({"op":"terminalDctLength","terminal":"dep","value":20,"allowedPoints":["FAR"]})),RadTruth::No);
    assert_eq!(ctx.eval(&rad_json!({"op":"terminalDctLength","terminal":"dep","value":20,"allowedPoints":["OTHER"]})),RadTruth::Yes);
    pilot.flight_plan.as_mut().unwrap().route=Some("TEST1A FAR".into());
    let mut ctx=rad_context_for_test(&pilot,&points,&reference,&areas);
    assert_eq!(ctx.eval(&node),RadTruth::No);
    pilot.flight_plan.as_mut().unwrap().route=Some("ELSE1A FAR".into());
    let mut ctx=rad_context_for_test(&pilot,&points,&reference,&areas);
    ctx.procedure_ids.insert("ELSE1A".into());
    assert_eq!(ctx.eval(&node),RadTruth::Unknown);
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn rad_native_reference_distinguishes_incoming_dct_and_first_airway_segment() {
    let _guard=RAD_NATIVE_TEST_LOCK.lock().unwrap();
    let (state,dir,input)=flight_tools_fixture();
    let mut reference=(*state.reference_data_snapshot()).clone();
    let mut nav=(*reference.navdata).clone();
    for (i,id) in ["AAA","BBB","CCC","DDD"].iter().enumerate() {
        nav.points.insert((*id).into(),Point{lat:0.5,lon:i as f64});
    }
    nav.airways.insert("R1".into(),vec!["BBB".into(),"CCC".into(),"DDD".into()]);
    reference.navdata=Arc::new(nav);
    let mut pilot=manual_fpl_pilot(serde_json::from_value(input).unwrap(),&reference.navdata).unwrap();
    pilot.flight_plan.as_mut().unwrap().route=Some("AAA DCT BBB R1 DDD".into());
    let mut points=(0..4).map(|i|rad_test_point(i as f64,25000.,i*10)).collect::<Vec<_>>();
    for (p,id) in points.iter_mut().zip(["AAA","BBB","CCC","DDD"]) {p.name=id.into();}
    let areas=SpecialAreaCache::default();
    let mut ctx=rad_context_for_test(&pilot,&points,&reference,&areas);
    let mut direct=rad_json!({"op":"dctAtReference","direction":"entry","location":{"op":"via","values":["BBB"]}});
    assert_eq!(ctx.eval(&direct),RadTruth::Yes);
    direct["direction"]="exit".into();assert_eq!(ctx.eval(&direct),RadTruth::No);
    let first=rad_json!({"op":"firstSegment","location":{"op":"route","parts":[["BBB"],["R1"],["DDD"]]}});
    assert_eq!(ctx.events(&first),(vec![(10.,20.)],false));
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn rad_native_activation_requires_complete_matching_source_and_fua_assignment() {
    let _guard=RAD_NATIVE_TEST_LOCK.lock().unwrap();
    let (state,dir,input)=flight_tools_fixture();
    let reference=state.reference_data_snapshot();
    let pilot=manual_fpl_pilot(serde_json::from_value(input).unwrap(),&reference.navdata).unwrap();
    let points=[rad_test_point(0.,15000.,0),rad_test_point(1.,15000.,60)];
    let areas=SpecialAreaCache::default();
    let mut ctx=rad_context_for_test(&pilot,&points,&reference,&areas);
    let node=rad_json!({"op":"nativeActivation","sourceRef":{"backendId":"AREA","uuid":"v1"},
        "offsetStartMinutes":0,"offsetEndMinutes":0,"vertical":true,"requested":false,"fua":true});
    assert_eq!(ctx.eval(&node),RadTruth::Unknown);
    ctx.rule_id="RULE".into();
    let mut support=rad_json!({"validFrom":"2026-11-05T00:00:00Z","validUntil":"2026-11-06T00:00:00Z",
        "nativeActivations":{"AREA":{"source":"NM test","sourceRef":node["sourceRef"],"complete":true,
        "validFrom":"2026-11-05T00:00:00Z","validUntil":"2026-11-06T00:00:00Z",
        "windows":[{"validFrom":"2026-11-05T12:00:00Z","validUntil":"2026-11-05T13:00:00Z",
        "minFt":15000,"maxFt":20000,"restrictionIds":["RULE"]}]}}});
    ctx.support=Arc::new(support.clone());
    assert_eq!(ctx.eval(&node),RadTruth::Yes);
    support["nativeActivations"]["AREA"]["windows"][0]["restrictionIds"]=rad_json!([]);
    ctx.support=Arc::new(support.clone());assert_eq!(ctx.eval(&node),RadTruth::No);
    support["nativeActivations"]["AREA"]["complete"]=false.into();
    ctx.support=Arc::new(support);assert_eq!(ctx.eval(&rad_json!({"op":"not","args":[node]})),RadTruth::Unknown);
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn rad_native_airspace_slice_can_extend_outside_source_vertical_volume() {
    let _guard=RAD_NATIVE_TEST_LOCK.lock().unwrap();
    let (state,dir,input)=flight_tools_fixture();
    let mut reference=(*state.reference_data_snapshot()).clone();
    reference.sectors=serde_json::from_value(rad_json!([{"id":"TESTCTA","blocks":[{
        "floorFt":20000,"ceilingFt":30000,"polygon":[{"lat":0.,"lon":0.},{"lat":0.,"lon":2.},
        {"lat":1.,"lon":2.},{"lat":1.,"lon":0.}]}]}])).unwrap();
    let pilot=manual_fpl_pilot(serde_json::from_value(input).unwrap(),&reference.navdata).unwrap();
    let points=[rad_test_point(-1.,10000.,0),rad_test_point(3.,10000.,40)];
    let areas=SpecialAreaCache::default();
    let mut ctx=rad_context_for_test(&pilot,&points,&reference,&areas);
    assert_eq!(ctx.eval(&rad_json!({"op":"via","values":["TESTCTA"]})),RadTruth::No);
    let slice=rad_json!({"op":"level","comparison":"BLW","value":15000,
        "location":{"op":"airspaceFootprint","values":["TESTCTA"]},"requested":false});
    assert_eq!(ctx.eval(&slice),RadTruth::Yes);
    assert_eq!(ctx.events(&slice).0,vec![(10.,30.)]);
    let mut missing=slice.clone();missing["location"]["values"][0]="MISSING".into();
    assert_eq!(ctx.eval(&missing),RadTruth::Unknown);
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn rad_native_dct_level_checks_entry_and_reference_endpoints() {
    let _guard=RAD_NATIVE_TEST_LOCK.lock().unwrap();
    let (state,dir,input)=flight_tools_fixture();
    let reference=state.reference_data_snapshot();
    let mut pilot=manual_fpl_pilot(serde_json::from_value(input).unwrap(),&reference.navdata).unwrap();
    pilot.flight_plan.as_mut().unwrap().route=Some("AAA DCT BBB".into());
    let mut points=[rad_test_point(0.,25000.,10),rad_test_point(1.,35000.,20)];
    points[0].name="AAA".into();points[1].name="BBB".into();
    let areas=SpecialAreaCache::default();
    let mut ctx=rad_context_for_test(&pilot,&points,&reference,&areas);
    let mut node=rad_json!({"op":"segmentLevel","location":{"op":"route","parts":[["AAA"],["DCT"],["BBB"]]},
        "checks":[{"comparison":"ABV","value":19500},{"comparison":"BLW","value":28500}],"sample":"entry","requested":false});
    assert_eq!(ctx.eval(&node),RadTruth::Yes);
    node["sample"]="endpoints".into();
    assert_eq!(ctx.eval(&node),RadTruth::No);
    node["sample"]="entry".into();node["checks"][0]["value"]=25000.into();
    assert_eq!(ctx.eval(&node),RadTruth::No);
    node["checks"][0]["value"]=19500.into();
    node["location"]["parts"][1][0]="OTHER".into();
    assert_eq!(ctx.eval(&node),RadTruth::No);
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn rad_native_terminal_airspace_uses_ground_projection_and_terminal_time() {
    let _guard = RAD_NATIVE_TEST_LOCK.lock().unwrap();
    let (state, dir, input) = flight_tools_fixture();
    let mut reference = (*state.reference_data_snapshot()).clone();
    let pilot = manual_fpl_pilot(serde_json::from_value(input).unwrap(), &reference.navdata).unwrap();
    let departure = pilot.flight_plan.as_ref().unwrap().departure_id.as_ref().unwrap();
    let position = reference.navdata.airports[departure].position;
    reference.sectors=serde_json::from_value(rad_json!([{"id":"TESTCTA","blocks":[{
        "floorFt":20000,"ceilingFt":30000,"polygon":[
            {"lat":position.lat-0.1,"lon":position.lon-0.1},
            {"lat":position.lat-0.1,"lon":position.lon+0.1},
            {"lat":position.lat+0.1,"lon":position.lon+0.1},
            {"lat":position.lat+0.1,"lon":position.lon-0.1}]}]}])).unwrap();
    let points=[rad_test_point(0.,0.,0),rad_test_point(1.,0.,60)];
    let areas=SpecialAreaCache::default();
    let mut ctx=rad_context_for_test(&pilot,&points,&reference,&areas);
    let node=rad_json!({"op":"airportInAirspace","terminal":"dep","values":["TESTCTA"]});
    assert_eq!(ctx.eval(&node),RadTruth::Yes);
    assert_eq!(ctx.events(&node),(vec![(0.,0.)],false));
    assert_eq!(ctx.eval(&rad_json!({"op":"airportInAirspace","terminal":"arr","values":["TESTCTA"]})),RadTruth::No);
    assert_eq!(ctx.eval(&rad_json!({"op":"airportInAirspace","terminal":"dep","values":["MISSING"]})),RadTruth::Unknown);
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn rad_native_point_band_keeps_bounds_and_occurrences_correlated() {
    let _guard = RAD_NATIVE_TEST_LOCK.lock().unwrap();
    let (state, dir, input) = flight_tools_fixture();
    let reference=state.reference_data_snapshot();
    let pilot=manual_fpl_pilot(serde_json::from_value(input).unwrap(),&reference.navdata).unwrap();
    let areas=SpecialAreaCache::default();
    let band=rad_json!({"op":"level","comparison":"BLW","value":28500,"requested":false,
        "location":{"op":"level","comparison":"ABV","value":19500,"requested":false,
                    "location":{"op":"via","values":["A"]}}});
    for (height,expected) in [(19500.,RadTruth::No),(28500.,RadTruth::No),(25000.,RadTruth::Yes)] {
        let mut points=[rad_test_point(0.,height,10)];points[0].name="A".into();
        let mut ctx=rad_context_for_test(&pilot,&points,&reference,&areas);
        assert_eq!(ctx.eval(&band),expected);
    }
    let mut points=[rad_test_point(0.,10000.,10),rad_test_point(1.,35000.,50)];
    for p in &mut points {p.name="A".into();}
    let mut ctx=rad_context_for_test(&pilot,&points,&reference,&areas);
    assert_eq!(ctx.eval(&band),RadTruth::No);
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn rad_native_loader_rejects_partial_catalogue_and_selects_validated_native() {
    let _guard = RAD_NATIVE_TEST_LOCK.lock().unwrap();
    let (state,dir,_) = flight_tools_fixture();
    install_rad_test_catalogue(&state,&dir,rad_json!([]));
    assert_eq!(state.rad_catalogue().unwrap().version,"1.22");
    let mut native=rad_json!({"schema":"iatfm-rad/1","airac":"2611","version":"native-test",
        "validFrom":"2026-10-29","validUntil":"2026-11-26","sourceKind":"nmp-database",
        "rules":[{"id":"TEST"}],"operationallyValidated":false,"globalPendingValidation":[],
        "summary":{"rules":1,"missingRestrictions":1,"rulesWithPendingSemantics":0}});
    write_data_file(&dir,"rad/catalogue.database.active.json",&native.to_string());
    assert!(state.rad_catalogue().is_err());
    native["operationallyValidated"]=true.into();native["summary"]["missingRestrictions"]=0.into();
    write_data_file(&dir,"rad/catalogue.database.active.json",&native.to_string());
    *state.rad_cache.lock().unwrap()=RadCache::default();
    assert_eq!(state.rad_catalogue().unwrap().version,"native-test");
    native["rules"][0]["scope"]=rad_json!({"op":"not","args":[{"op":"unknown"}]});
    write_data_file(&dir,"rad/catalogue.database.active.json",&native.to_string());
    *state.rad_cache.lock().unwrap()=RadCache::default();
    assert!(state.rad_catalogue().is_err());
    native["allowIncompleteNative"]=true.into();native["operationallyValidated"]=false.into();
    native["summary"]["rulesWithPendingSemantics"]=1.into();
    write_data_file(&dir,"rad/catalogue.database.active.json",&native.to_string());
    *state.rad_cache.lock().unwrap()=RadCache::default();
    let loaded=state.rad_catalogue().unwrap();
    assert_eq!(loaded.version,"native-test");assert!(!loaded.operationally_validated);
    native["summary"]["missingRestrictions"]=1.into();
    write_data_file(&dir,"rad/catalogue.database.active.json",&native.to_string());
    *state.rad_cache.lock().unwrap()=RadCache::default();assert!(state.rad_catalogue().is_err());
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn rad_native_midnight_revision_is_selected_at_arrival_not_departure() {
    let _guard = RAD_NATIVE_TEST_LOCK.lock().unwrap();
    let (state, dir, input) = flight_tools_fixture();
    let reference = state.reference_data_snapshot();
    let pilot = manual_fpl_pilot(serde_json::from_value(input).unwrap(), &reference.navdata).unwrap();
    let airport = pilot.flight_plan.as_ref().unwrap().arrival_id.as_ref().unwrap();
    let midnight: DateTime<Utc> = "2026-11-06T00:00:00Z".parse().unwrap();
    let ms = midnight.timestamp_millis();
    let rule = |key: &str, start, end, day: &str| rad_json!({
        "id":"NATIVE","sheet":"NMP:NATIVE","key":key,"validFrom":day,
        "scope":{"op":"arr","values":[airport]},
        "referenceLocation":{"op":"arr","values":[airport]},
        "sourceValidity":{"start":start,"end":end},
        "schedule":{"op":"timeAt","location":{"op":"arr","values":[airport]},
                    "schedule":{"op":"time","start":15,"end":60}},
        "clauses":[{"action":"forbidden","condition":{"op":"true"}}]
    });
    install_rad_test_catalogue(&state, &dir, rad_json!([
        rule("old",ms-86400000,ms,"2026-11-05"),
        rule("new",ms,ms+86400000,"2026-11-06")
    ]));
    let points = [rad_test_point(0.,10000.,0),rad_test_point(1.,0.,60)];
    let (issues, meta) = state.check_rad_and_areas(&pilot,&points,&reference,midnight-ChronoDuration::minutes(30));
    assert_eq!(issues.iter().filter(|v|v["ruleId"]=="NATIVE" && v["status"]=="error").count(),1,"{issues:?}");
    assert_eq!(meta["evaluatedRules"],2);
    // Arrival exactly at the new state's end must not match it.
    let (issues, _) = state.check_rad_and_areas(&pilot,&points,&reference,midnight+ChronoDuration::days(1)-ChronoDuration::minutes(60));
    assert!(!issues.iter().any(|v|v["ruleId"]=="NATIVE" && v["status"]=="error"));
    fs::remove_dir_all(dir).unwrap();
}

#[test]
fn rad_native_state_and_schedule_must_match_the_same_reference_event() {
    let _guard = RAD_NATIVE_TEST_LOCK.lock().unwrap();
    let (state, dir, input) = flight_tools_fixture();
    let reference = state.reference_data_snapshot();
    let pilot = manual_fpl_pilot(serde_json::from_value(input).unwrap(), &reference.navdata).unwrap();
    let mut points = [rad_test_point(0.,10000.,10),rad_test_point(1.,10000.,50)];
    for p in &mut points { p.name="REPEAT".into(); }
    let areas = SpecialAreaCache::default();
    let mut ctx = rad_context_for_test(&pilot,&points,&reference,&areas);
    let ms=ctx.takeoff.timestamp_millis();
    let rule=rad_json!({"sourceValidity":{"start":ms,"end":ms+30*60000},
        "referenceLocation":{"op":"via","values":["REPEAT"]},
        "schedule":{"op":"timeAt","location":{"op":"via","values":["REPEAT"]},
                    "schedule":{"op":"time","start":760,"end":780}}});
    assert_eq!(ctx.database_schedule(&rule),RadTruth::No);
    fs::remove_dir_all(dir).unwrap();
}
