def calculate_confidence(source, match_type):
    source = (source or "").lower()
    match_type = (match_type or "").lower()

    if "rooftop" in match_type:
        return 100

    if "parcel" in match_type:
        return 95

    if "address" in match_type:
        return 90

    if "street" in match_type:
        return 75

    if "zip" in match_type:
        return 50

    if source == "census":
        return 85

    if source == "arcgis":
        return 90

    return 60
