"""Ruteplanlegging for BNC-selgeren.

Enkel, rask heuristikk: grådig nærmeste-nabo fra startposisjon, der vi legger til
besøk så lenge (kjøretid + besøkstid) holder seg innenfor dagens tidsbudsjett.

Kjøretid estimeres fra luftlinje × omvei-faktor / snittfart (ingen API-nøkkel).
Kan senere byttes med ekte kjøretid uten at resten av appen endres.
"""
from math import radians, sin, cos, asin, sqrt

DEFAULT_SPEED_KMH = 70.0      # snittfart bil (blandet vei)
DEFAULT_DETOUR = 1.3          # luftlinje → faktisk vei (omvei-faktor)


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * r * asin(sqrt(a))


def travel_minutes(km, speed_kmh=DEFAULT_SPEED_KMH, detour=DEFAULT_DETOUR):
    if speed_kmh <= 0:
        return 0.0
    return (km * detour) / speed_kmh * 60.0


def plan_route(start_lat, start_lon, candidates, hours_available,
               visit_minutes=60.0, speed_kmh=DEFAULT_SPEED_KMH, detour=DEFAULT_DETOUR,
               max_leg_km=None, return_to_start=False, max_stops=None, max_radius_km=None):
    """Foreslå en besøksrute (grådig nærmeste nabo).

    Styres av `max_stops` (antall besøk) når den er satt – da ignoreres tidsbudsjettet,
    slik at «antall besøk» bestemmer (Kim tar de han rekker). Er `max_stops` None, brukes
    tidsbudsjettet `hours_available` som før.

    `max_radius_km` begrenser forslagene til en radius fra STARTEN (for base/område-søk,
    så en rute rundt basen ikke kjeder seg langt av gårde). `max_leg_km` begrenser
    avstanden mellom to påfølgende besøk.

    candidates: liste av dict med minst 'lat' og 'lon' (og gjerne 'orgnr','navn').
    Returnerer dict: route, total_min, total_km, skipped, n_candidates.
    """
    budget = float(hours_available) * 60.0
    pool = [c for c in candidates
            if c.get("lat") is not None and c.get("lon") is not None]
    if max_radius_km:
        pool = [c for c in pool
                if haversine_km(float(start_lat), float(start_lon),
                                float(c["lat"]), float(c["lon"])) <= max_radius_km]
    cur_lat, cur_lon = float(start_lat), float(start_lon)
    used_min = 0.0
    total_km = 0.0
    route = []
    chosen = set()

    while True:
        if max_stops and len(route) >= int(max_stops):
            break
        best = None
        best_km = None
        for i, c in enumerate(pool):
            if i in chosen:
                continue
            km = haversine_km(cur_lat, cur_lon, float(c["lat"]), float(c["lon"]))
            if max_leg_km and km > max_leg_km:
                continue
            if best_km is None or km < best_km:
                best_km, best = km, i
        if best is None:
            break
        c = pool[best]
        leg_min = travel_minutes(best_km, speed_kmh, detour)
        # Reservér tid for retur til start hvis ønsket (grov sjekk mot neste punkt).
        back_min = 0.0
        if return_to_start:
            back_km = haversine_km(float(c["lat"]), float(c["lon"]), start_lat, start_lon)
            back_min = travel_minutes(back_km, speed_kmh, detour)
        if max_stops is None and used_min + leg_min + visit_minutes + back_min > budget:
            # Tidsbudsjett-modus: dette besøket sprenger dagen – stopp.
            # (Når max_stops styrer, ignoreres budsjettet – antall besøk bestemmer.)
            break
        used_min += leg_min + visit_minutes
        total_km += best_km
        chosen.add(best)
        route.append({
            **c,
            "leg_km": round(best_km, 1),
            "leg_min": round(leg_min),
            "visit_min": round(visit_minutes),
            "cum_min": round(used_min),
            "stop": len(route) + 1,
        })
        cur_lat, cur_lon = float(c["lat"]), float(c["lon"])

    if return_to_start and route:
        back_km = haversine_km(cur_lat, cur_lon, start_lat, start_lon)
        used_min += travel_minutes(back_km, speed_kmh, detour)
        total_km += back_km

    return {
        "route": route,
        "total_min": round(used_min),
        "total_km": round(total_km, 1),
        "skipped": len(pool) - len(route),
        "n_candidates": len(pool),
    }
