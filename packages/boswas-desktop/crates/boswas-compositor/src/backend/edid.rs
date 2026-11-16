//! The two facts the desktop shows about a monitor, read from its EDID:
//! who made it and what it is called.

/// A monitor's maker (the three-letter PNP id, or a well-known name for
/// it) and its model name, when the EDID has them.
pub fn make_and_model(edid: &[u8]) -> (Option<String>, Option<String>) {
    const HEADER: [u8; 8] = [0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x00];
    if edid.len() < 128 || edid[..8] != HEADER {
        return (None, None);
    }
    // Manufacturer: three letters packed into two bytes, five bits each.
    let packed = u16::from_be_bytes([edid[8], edid[9]]);
    let letter = |shift: u16| {
        let value = ((packed >> shift) & 0x1F) as u8;
        (1..=26).contains(&value).then(|| (b'A' + value - 1) as char)
    };
    let make = match (letter(10), letter(5), letter(0)) {
        (Some(a), Some(b), Some(c)) => {
            let id: String = [a, b, c].iter().collect();
            Some(maker_name(&id).map_or(id, str::to_owned))
        }
        _ => None,
    };
    // Model: the display descriptor tagged 0xFC, text ended by a line feed.
    let model = (54..=108).step_by(18).find_map(|offset| {
        let d = &edid[offset..offset + 18];
        if d[0] != 0 || d[1] != 0 || d[2] != 0 || d[3] != 0xFC {
            return None;
        }
        let text: String = d[5..18]
            .iter()
            .take_while(|byte| **byte != b'\n')
            .filter(|byte| byte.is_ascii_graphic() || **byte == b' ')
            .map(|byte| *byte as char)
            .collect();
        let text = text.trim().to_owned();
        (!text.is_empty()).then_some(text)
    });
    (make, model)
}

/// The names of the makers found most often in the wild.
fn maker_name(id: &str) -> Option<&'static str> {
    Some(match id {
        "ACI" | "AUS" => "ASUS",
        "ACR" => "Acer",
        "AOC" => "AOC",
        "APP" => "Apple",
        "AUO" => "AU Optronics",
        "BNQ" => "BenQ",
        "BOE" => "BOE",
        "CMN" => "Chimei Innolux",
        "DEL" => "Dell",
        "GSM" => "LG",
        "HPN" | "HWP" => "HP",
        "HUA" => "Huawei",
        "IVM" => "Iiyama",
        "LEN" => "Lenovo",
        "LGD" => "LG Display",
        "MSI" => "MSI",
        "PHL" => "Philips",
        "SAM" => "Samsung",
        "SDC" => "Samsung Display",
        "SHP" => "Sharp",
        "SNY" => "Sony",
        "VSC" => "ViewSonic",
        "XMI" => "Xiaomi",
        _ => return None,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn edid(maker: [u8; 2], name: Option<&str>) -> Vec<u8> {
        let mut data = vec![0u8; 128];
        data[..8].copy_from_slice(&[0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x00]);
        data[8..10].copy_from_slice(&maker);
        if let Some(name) = name {
            let d = &mut data[72..90];
            d[3] = 0xFC;
            let bytes = name.as_bytes();
            d[5..5 + bytes.len()].copy_from_slice(bytes);
            if bytes.len() < 13 {
                d[5 + bytes.len()] = b'\n';
                for byte in &mut d[6 + bytes.len()..] {
                    *byte = b' ';
                }
            }
        }
        data
    }

    #[test]
    fn maker_and_name_are_decoded() {
        // "DEL" = 4, 5, 12 -> 00100 00101 01100.
        let dell = edid([0x10, 0xAC], Some("DELL U2720Q"));
        assert_eq!(make_and_model(&dell), (Some("Dell".into()), Some("DELL U2720Q".into())));
        // An id without a well-known name is shown as it is: "XYZ".
        let packed: u16 = (24 << 10) | (25 << 5) | 26;
        let other = edid(packed.to_be_bytes(), Some("Thirteen char"));
        assert_eq!(make_and_model(&other), (Some("XYZ".into()), Some("Thirteen char".into())));
    }

    #[test]
    fn missing_or_broken_data_gives_nothing() {
        assert_eq!(make_and_model(&[]), (None, None));
        assert_eq!(make_and_model(&[0u8; 128]), (None, None), "no EDID header");
        let no_name = edid([0x10, 0xAC], None);
        assert_eq!(make_and_model(&no_name), (Some("Dell".into()), None));
        let bad_maker = edid([0x00, 0x00], Some("X"));
        assert_eq!(make_and_model(&bad_maker).0, None);
        let mut control = edid([0x10, 0xAC], Some("AB"));
        control[77] = 0x07;
        assert_eq!(make_and_model(&control).1.as_deref(), Some("B"), "control characters are dropped");
    }
}
