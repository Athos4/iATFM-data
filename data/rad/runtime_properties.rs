// Structured EUROCONTROL flight-property predicates. Missing flight data stays
// indeterminate, including when the caller negates the predicate.
fn rad_database_terminal_ifr(rules: Option<&str>, route: Option<&str>, terminal: &str) -> RadTruth {
    if !matches!(terminal, "dep" | "arr") {
        return RadTruth::Unknown;
    }
    let Some(rules) = rules else { return RadTruth::Unknown; };
    let rules = rules.trim().to_ascii_uppercase();
    let initial = match rules.as_str() {
        "I" | "IFR" | "Y" => true,
        "V" | "VFR" | "Z" => false,
        _ => return RadTruth::Unknown,
    };
    if terminal == "dep" {
        return rad_bool(initial);
    }
    let mixed = matches!(rules.as_str(), "Y" | "Z");
    let mut current = initial;
    let mut changed = false;
    for token in route.unwrap_or("").split_whitespace() {
        let next = match token.to_ascii_uppercase().as_str() {
            "IFR" => Some(true),
            "VFR" => Some(false),
            _ => None,
        };
        if let Some(next) = next {
            changed |= next != current;
            current = next;
        }
    }
    if mixed != changed {
        return RadTruth::Unknown;
    }
    rad_bool(current)
}

fn rad_database_equipment(actual: Option<&str>, wanted: &[&str]) -> RadTruth {
    let Some(actual) = actual.filter(|s| !s.trim().is_empty()) else {
        return RadTruth::Unknown;
    };
    let actual = actual.trim().to_ascii_uppercase();
    if !actual.bytes().all(|b| b.is_ascii_alphanumeric()) || wanted.is_empty() {
        return RadTruth::Unknown;
    }
    let bytes = actual.as_bytes();
    let mut tokens = Vec::new();
    let mut index = 0;
    while index < bytes.len() {
        if !bytes[index].is_ascii_uppercase() {
            return RadTruth::Unknown;
        }
        let start = index;
        index += 1;
        if index < bytes.len() && bytes[index].is_ascii_digit() {
            index += 1;
        }
        tokens.push(&actual[start..index]);
    }
    if tokens.contains(&"N") && tokens.len() != 1 {
        return RadTruth::Unknown;
    }
    if wanted.iter().any(|value| tokens.contains(value)) {
        return RadTruth::Yes;
    }
    // ICAO standard equipment shorthand needs expansion for these predicates.
    // Do not turn an unexpanded shorthand into an equipment absence.
    if tokens.contains(&"S") && wanted.iter().any(|v| matches!(*v, "V" | "O" | "L")) {
        return RadTruth::Unknown;
    }
    RadTruth::No
}

fn rad_database_registration(actual: Option<&str>, wanted: &[&str]) -> RadTruth {
    let Some(actual) = actual.filter(|s| !s.trim().is_empty()) else {
        return RadTruth::Unknown;
    };
    if wanted.is_empty() || wanted.iter().any(|s| !s.bytes().all(|b| b.is_ascii_alphanumeric())) {
        return RadTruth::Unknown;
    }
    let actual = actual.trim().to_ascii_uppercase().replace('-', "");
    if !actual.bytes().all(|b| b.is_ascii_alphanumeric()) {
        return RadTruth::Unknown;
    }
    rad_bool(wanted.iter().any(|value| actual == *value))
}

impl RadContext<'_> {
    fn eval_database_property(&self, node: &RadValue) -> RadTruth {
        let Some(plan) = self.pilot.flight_plan.as_ref() else {
            return RadTruth::Unknown;
        };
        let values = rad_values(node);
        match rad_str(node, "op") {
            "flightRulesAtTerminal" => rad_database_terminal_ifr(
                plan.flight_rules.as_deref(), plan.route.as_deref(), rad_str(node, "terminal"),
            ),
            "registration" => {
                let field = plan.registration.as_deref();
                let remarks = plan.remarks.as_deref().and_then(|s| {
                    s.split_whitespace().find_map(|part| part.strip_prefix("REG/"))
                });
                if let (Some(a), Some(b)) = (field, remarks)
                    && a.trim().to_ascii_uppercase().replace('-', "")
                        != b.trim().to_ascii_uppercase().replace('-', "")
                {
                    return RadTruth::Unknown;
                }
                rad_database_registration(field.or(remarks), &values)
            }
            "comNavEquipment" => rad_database_equipment(
                plan.equipment.as_deref().and_then(|s| s.split('/').next()), &values,
            ),
            "surveillanceEquipment" => {
                let field = plan.surveillance.as_deref();
                let combined = plan.equipment.as_deref().and_then(|s| s.split_once('/').map(|(_, s)| s));
                if let (Some(a), Some(b)) = (field, combined)
                    && a.trim().to_ascii_uppercase() != b.trim().to_ascii_uppercase()
                {
                    return RadTruth::Unknown;
                }
                rad_database_equipment(field.or(combined), &values)
            }
            _ => RadTruth::Unknown,
        }
    }
}

#[cfg(test)]
mod rad_database_property_tests {
    use super::*;

    #[test]
    fn mixed_rules_distinguish_departure_and_arrival_phases() {
        assert_eq!(rad_database_terminal_ifr(Some("Y"), None, "dep"), RadTruth::Yes);
        assert_eq!(rad_database_terminal_ifr(Some("Z"), None, "dep"), RadTruth::No);
        assert_eq!(rad_database_terminal_ifr(Some("Y"), Some("AAA VFR BBB"), "arr"), RadTruth::No);
        assert_eq!(rad_database_terminal_ifr(Some("Y"), Some("AAA VFR BBB IFR CCC"), "arr"), RadTruth::Yes);
        assert_eq!(rad_database_terminal_ifr(Some("Z"), Some("AAA IFR BBB"), "arr"), RadTruth::Yes);
        assert_eq!(rad_database_terminal_ifr(Some("Y"), Some("AAA DCT BBB"), "arr"), RadTruth::Unknown);
        assert_eq!(rad_database_terminal_ifr(Some("I"), Some("AAA VFR BBB"), "arr"), RadTruth::Unknown);
        assert_eq!(rad_database_terminal_ifr(Some("I"), None, "arr"), RadTruth::Yes);
        assert_eq!(rad_database_terminal_ifr(None, None, "arr"), RadTruth::Unknown);
    }

    #[test]
    fn equipment_tokens_do_not_confuse_codes_and_prefixes() {
        assert_eq!(rad_database_equipment(Some("SDFGJ5"), &["J5"]), RadTruth::Yes);
        assert_eq!(rad_database_equipment(Some("B1D1"), &["B"]), RadTruth::No);
        assert_eq!(rad_database_equipment(Some("B1D1"), &["B1"]), RadTruth::Yes);
        assert_eq!(rad_database_equipment(None, &["D1"]), RadTruth::Unknown);
        assert_eq!(rad_database_equipment(Some("D12"), &["D1"]), RadTruth::Unknown);
        assert_eq!(rad_database_equipment(Some("ND1"), &["D1"]), RadTruth::Unknown);
        assert_eq!(rad_database_equipment(Some("S"), &["V"]), RadTruth::Unknown);
    }

    #[test]
    fn registration_normalizes_display_hyphens_but_preserves_missing_data() {
        assert_eq!(rad_database_registration(Some("es-lsa"), &["ESLSA"]), RadTruth::Yes);
        assert_eq!(rad_database_registration(Some("ESLSB"), &["ESLSA"]), RadTruth::No);
        assert_eq!(rad_database_registration(None, &["ESLSA"]).not(), RadTruth::Unknown);
        assert_eq!(rad_database_registration(Some("ESLSA"), &["ES*"]), RadTruth::Unknown);
    }
}
