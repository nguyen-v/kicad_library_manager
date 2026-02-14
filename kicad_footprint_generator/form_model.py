"""
Pure (non-GUI) helpers for the footprint generator GUI(s).

This intentionally contains **no tkinter/wx** imports so it can be reused inside
KiCad (wxPython) and also by any future CLI / tests.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .generate import DEFAULT_SETTINGS, build_pattern


# Keep in sync with the original tkinter GUI list.
KINDS: List[str] = [
    "soic",
    "sop",
    "sopfl",
    "sotfl",
    "soj",
    "sol",
    "son",
    "sot23",
    "sot223",
    "sot143",
    "sot89_5",
    "dip",
    "pak",
    "qfp",
    "qfn",
    "pqfn",
    "cqfp",
    "bga",
    "lga",
    "cga",
    "chip",
    "chip_array",
    "oscillator",
    "crystal",
    "cae",
    "melf",
    "molded",
    "pson",
    "dfn",
    "radial",
    "sod",
    "sodfl",
    "mounting_hole",
    "bridge",
    # "custom" exists but is currently advanced/manual (JSON-like); omitted for now.
]


FieldSpec = Tuple[str, str, Any, Optional[List[Any]]]
# (label, path, default, choices?)


def schema_for_kind(kind: str) -> List[FieldSpec]:
    """
    Return a list of field definitions: (label, dotted_path, default, choices?).

    Ported from the original tkinter GUI logic, with minor fixes:
    - Boolean fields are explicitly booleans (so wx can use checkboxes)
    """

    def rng(prefix: str, nom: str, mi: str, ma: str) -> List[FieldSpec]:
        return [
            (f"{prefix} nom", f"{nom}", 0.0, None),
            (f"{prefix} min", f"{mi}", 0.0, None),
            (f"{prefix} max", f"{ma}", 0.0, None),
        ]

    if kind == "oscillator":
        return [
            ("Variant", "variant", "corner-concave", ["corner-concave", "side-concave", "side-flat"]),
            ("Lead count", "leadCount", 4, None),
            ("Body width nom", "bodyWidth.nom", 3.2, None),
            ("Body width min", "bodyWidth.min", 3.1, None),
            ("Body width max", "bodyWidth.max", 3.3, None),
            ("Body length nom", "bodyLength.nom", 2.5, None),
            ("Body length min", "bodyLength.min", 2.4, None),
            ("Body length max", "bodyLength.max", 2.6, None),
            ("Height max", "height.max", 1.2, None),
            ("Pad separation width nom", "padSeparationWidth.nom", 2.2, None),
            ("Pad separation width min", "padSeparationWidth.min", 2.1, None),
            ("Pad separation width max", "padSeparationWidth.max", 2.3, None),
            ("Pad separation length nom", "padSeparationLength.nom", 1.8, None),
            ("Pad separation length min", "padSeparationLength.min", 1.7, None),
            ("Pad separation length max", "padSeparationLength.max", 1.9, None),
        ]

    if kind == "pak":
        return (
            [("Lead count", "leadCount", 3, None), ("Pitch", "pitch", 2.29, None)]
            + rng("Lead span", "leadSpan.nom", "leadSpan.min", "leadSpan.max")
            + [
                ("Lead length min", "leadLength.min", 0.4, None),
                ("Lead length max", "leadLength.max", 0.6, None),
                ("Lead width min", "leadWidth.min", 0.3, None),
                ("Lead width max", "leadWidth.max", 0.5, None),
                ("Body width max", "bodyWidth.max", 6.5, None),
                ("Body length max", "bodyLength.max", 6.5, None),
                ("Height max", "height.max", 2.3, None),
                ("Tab width min", "tabWidth.min", 3.0, None),
                ("Tab width nom", "tabWidth.nom", 3.5, None),
                ("Tab width max", "tabWidth.max", 4.0, None),
                ("Tab length min", "tabLength.min", 4.5, None),
                ("Tab length nom", "tabLength.nom", 5.0, None),
                ("Tab length max", "tabLength.max", 5.5, None),
                ("Tab ledge min", "tabLedge.min", 0.5, None),
            ]
        )

    if kind == "sot23":
        return [
            ("Lead count", "leadCount", 6, None),
            ("Pitch", "pitch", 0.95, None),
            ("Lead span nom", "leadSpan.nom", 2.8, None),
            ("Lead span min", "leadSpan.min", 2.55, None),
            ("Lead span max", "leadSpan.max", 3.05, None),
            ("Lead length nom", "leadLength.nom", 0.45, None),
            ("Lead length min", "leadLength.min", 0.3, None),
            ("Lead length max", "leadLength.max", 0.6, None),
            ("Lead width nom", "leadWidth.nom", 0.4, None),
            ("Lead width min", "leadWidth.min", 0.3, None),
            ("Lead width max", "leadWidth.max", 0.5, None),
            ("Body width nom", "bodyWidth.nom", 1.6, None),
            ("Body width min", "bodyWidth.min", 1.45, None),
            ("Body width max", "bodyWidth.max", 1.75, None),
            ("Body length nom", "bodyLength.nom", 2.9, None),
            ("Body length min", "bodyLength.min", 2.75, None),
            ("Body length max", "bodyLength.max", 3.05, None),
            ("Height max", "height.max", 1.1, None),
        ]

    if kind == "sop":
        return [
            ("Lead count", "leadCount", 20, None),
            ("Pitch", "pitch", 0.5, None),
            ("Lead span nom", "leadSpan.nom", 4.9, None),
            ("Lead span min", "leadSpan.min", 4.7, None),
            ("Lead span max", "leadSpan.max", 5.1, None),
            ("Lead length min", "leadLength.min", 0.4, None),
            ("Lead length nom", "leadLength.nom", 0.55, None),
            ("Lead length max", "leadLength.max", 0.7, None),
            ("Lead width min", "leadWidth.min", 0.165, None),
            ("Lead width nom", "leadWidth.nom", 0.22, None),
            ("Lead width max", "leadWidth.max", 0.275, None),
            ("Body width nom", "bodyWidth.nom", 3.0, None),
            ("Body width min", "bodyWidth.min", 2.9, None),
            ("Body width max", "bodyWidth.max", 3.1, None),
            ("Body length nom", "bodyLength.nom", 5.1, None),
            ("Body length min", "bodyLength.min", 5.0, None),
            ("Body length max", "bodyLength.max", 5.2, None),
            ("Height max", "height.max", 1.1, None),
            ("Thermal pad width nom", "tabWidth.nom", 0.0, None),
            ("Thermal pad width min", "tabWidth.min", 0.0, None),
            ("Thermal pad width max", "tabWidth.max", 0.0, None),
            ("Thermal pad length nom", "tabLength.nom", 0.0, None),
            ("Thermal pad length min", "tabLength.min", 0.0, None),
            ("Thermal pad length max", "tabLength.max", 0.0, None),
        ]

    if kind == "soic":
        return [
            ("Lead count", "leadCount", 4, None),
            ("Pitch", "pitch", 2.5, None),
            ("Lead span nom", "leadSpan.nom", 6.4, None),
            ("Lead span min", "leadSpan.min", 6.1, None),
            ("Lead span max", "leadSpan.max", 6.7, None),
            ("Lead length nom", "leadLength.nom", 0.79, None),
            ("Lead length min", "leadLength.min", 0.48, None),
            ("Lead length max", "leadLength.max", 1.1, None),
            ("Lead width nom", "leadWidth.nom", 0.635, None),
            ("Lead width min", "leadWidth.min", 0.43, None),
            ("Lead width max", "leadWidth.max", 0.84, None),
            ("Body width nom", "bodyWidth.nom", 3.9, None),
            ("Body width min", "bodyWidth.min", 3.6, None),
            ("Body width max", "bodyWidth.max", 4.2, None),
            ("Body length nom", "bodyLength.nom", 4.725, None),
            ("Body length min", "bodyLength.min", 4.5, None),
            ("Body length max", "bodyLength.max", 4.95, None),
            ("Height max", "height.max", 2.9, None),
        ]

    if kind in ("sopfl", "sotfl", "soj", "sol", "sot143", "sot89_5"):
        if kind == "sotfl":
            base: List[FieldSpec] = [
                ("Component type", "componentType", "ICSOFL", ["ICSOFL", "TRXSOFL"]),
                ("Lead count", "leadCount", 3, [3, 5, 6]),
                ("Pitch", "pitch", 0.95, None),
                ("Lead span nom", "leadSpan.nom", 2.4, None),
                ("Lead span min", "leadSpan.min", 2.3, None),
                ("Lead span max", "leadSpan.max", 2.5, None),
            ]
        elif kind == "soj":
            base = [
                ("Lead count", "leadCount", 6, None),
                ("Pitch", "pitch", 1.1, None),
                ("Lead span nom", "leadSpan.nom", 3.45, None),
                ("Lead span min", "leadSpan.min", 3.25, None),
                ("Lead span max", "leadSpan.max", 3.65, None),
            ]
        else:
            base = [("Lead count", "leadCount", 8, None), ("Pitch", "pitch", 1.27, None)] + rng(
                "Lead span", "leadSpan.nom", "leadSpan.min", "leadSpan.max"
            )
        base += [
            ("Lead length min", "leadLength.min", 0.4 if kind == "soj" else (0.3 if kind == "sotfl" else 0.4), None),
            ("Lead length nom", "leadLength.nom", 0.7 if kind == "soj" else (0.4 if kind == "sotfl" else 0.5), None),
            ("Lead length max", "leadLength.max", 0.9 if kind == "soj" else (0.5 if kind == "sotfl" else 0.6), None),
            ("Lead width min", "leadWidth.min", 0.4 if kind == "soj" else (0.37 if kind == "sotfl" else 0.3), None),
            ("Lead width nom", "leadWidth.nom", 0.5 if kind == "soj" else (0.44 if kind == "sotfl" else 0.4), None),
            ("Lead width max", "leadWidth.max", 0.6 if kind == "soj" else 0.5, None),
            ("Body width nom", "bodyWidth.nom", 3.1 if kind == "soj" else (1.8 if kind == "sotfl" else 3.9), None),
            ("Body width min", "bodyWidth.min", 2.9 if kind == "soj" else (1.7 if kind == "sotfl" else 3.7), None),
            ("Body width max", "bodyWidth.max", 3.3 if kind == "soj" else (1.9 if kind == "sotfl" else 4.1), None),
            ("Body length nom", "bodyLength.nom", 3.3 if kind == "soj" else (2.9 if kind == "sotfl" else 4.9), None),
            ("Body length min", "bodyLength.min", 3.1 if kind == "soj" else (2.7 if kind == "sotfl" else 4.7), None),
            ("Body length max", "bodyLength.max", 3.5 if kind == "soj" else (3.1 if kind == "sotfl" else 5.1), None),
            ("Height max", "height.max", 2.24 if kind == "soj" else (0.88 if kind == "sotfl" else 1.75), None),
        ]
        if kind in ("sot223", "sot143", "sot89_5"):
            base += [
                ("Lead width1 min", "leadWidth1.min", 0.3, None),
                ("Lead width1 max", "leadWidth1.max", 0.5, None),
                ("Lead width2 min", "leadWidth2.min", 1.0, None),
                ("Lead width2 max", "leadWidth2.max", 2.0, None),
            ]
        return base

    if kind in ("son", "pson", "dfn"):
        if kind == "son":
            base = [
                ("Lead count", "leadCount", 14, None),
                ("Pitch (e)", "pitch", 0.4, None),
                ("Body width nom", "bodyWidth.nom", 2.9, None),
                ("Body width min", "bodyWidth.min", 2.8, None),
                ("Body width max", "bodyWidth.max", 3.0, None),
                ("Body length nom", "bodyLength.nom", 2.9, None),
                ("Body length min", "bodyLength.min", 2.8, None),
                ("Body length max", "bodyLength.max", 3.0, None),
                ("Lead length min", "leadLength.min", 0.2, None),
                ("Lead length max", "leadLength.max", 0.4, None),
                ("Lead width min", "leadWidth.min", 0.15, None),
                ("Lead width max", "leadWidth.max", 0.25, None),
                ("Pull back (nom)", "pullBack.nom", 0.0, None),
                ("Height max", "height.max", 0.8, None),
            ]
            base += [
                ("Thermal pad width nom", "tabWidth.nom", 1.7, None),
                ("Thermal pad width min", "tabWidth.min", 1.6, None),
                ("Thermal pad width max", "tabWidth.max", 1.8, None),
                ("Thermal pad length nom", "tabLength.nom", 2.3, None),
                ("Thermal pad length min", "tabLength.min", 2.2, None),
                ("Thermal pad length max", "tabLength.max", 2.4, None),
            ]
            return base

        # PSON / DFN defaults
        base = []
        if kind == "dfn":
            base.append(("Component type", "componentType", "capacitor", ["capacitor", "capacitor_polarized", "crystal", "diode", "diode_non_polarized", "fuse", "inductor", "led", "resistor", "transistor"]))
        base += [
            ("Lead count", "leadCount", 2, [2, 3, 4] if kind == "dfn" else None),
            ("Pitch (e)", "pitch", 0.0, None),
            ("Body length nom", "bodyLength.nom", 2.0, None),
            ("Body length min", "bodyLength.min", 1.8, None),
            ("Body length max", "bodyLength.max", 2.2, None),
            ("Body width nom", "bodyWidth.nom", 1.6, None),
            ("Body width min", "bodyWidth.min", 1.4, None),
            ("Body width max", "bodyWidth.max", 1.8, None),
            ("Lead length min", "leadLength.min", 0.4, None),
            ("Lead length max", "leadLength.max", 0.8, None),
            ("Lead width min", "leadWidth.min", 1.4, None),
            ("Lead width max", "leadWidth.max", 1.8, None),
            ("Pull back (nom)", "pullBack.nom", 0.0, None),
            ("Height max", "height.max", 1.0, None),
        ]
        if kind == "pson":
            base += [
                ("Thermal pad width nom", "tabWidth.nom", 0.0, None),
                ("Thermal pad width min", "tabWidth.min", 0.0, None),
                ("Thermal pad width max", "tabWidth.max", 0.0, None),
                ("Thermal pad length nom", "tabLength.nom", 0.0, None),
                ("Thermal pad length min", "tabLength.min", 0.0, None),
                ("Thermal pad length max", "tabLength.max", 0.0, None),
            ]
        if kind == "dfn":
            base += [
                ("Pitch along length (e1)", "pitch1", 1.4, None),
                ("Large pad offset (e2)", "pitch2", 0.0, None),
                ("Large pad width nom", "largePadWidth.nom", 1.2, None),
                ("Large pad width min", "largePadWidth.min", 1.0, None),
                ("Large pad width max", "largePadWidth.max", 1.4, None),
                ("Large pad length nom", "largePadLength.nom", 1.8, None),
                ("Large pad length min", "largePadLength.min", 1.6, None),
                ("Large pad length max", "largePadLength.max", 2.0, None),
            ]
        return base

    if kind in ("qfp", "cqfp"):
        return [
            ("Pitch", "pitch", 0.5, None),
            ("Row count", "rowCount", 16, None),
            ("Column count", "columnCount", 16, None),
            ("Row span nom", "rowSpan.nom", 12.0, None),
            ("Row span min", "rowSpan.min", 11.9, None),
            ("Row span max", "rowSpan.max", 12.1, None),
            ("Column span nom", "columnSpan.nom", 12.0, None),
            ("Column span min", "columnSpan.min", 11.9, None),
            ("Column span max", "columnSpan.max", 12.1, None),
            ("Body width nom", "bodyWidth.nom", 10.0, None),
            ("Body length nom", "bodyLength.nom", 10.0, None),
            ("Lead length nom", "leadLength.nom", 0.6, None),
            ("Lead length min", "leadLength.min", 0.5, None),
            ("Lead length max", "leadLength.max", 0.7, None),
            ("Lead width nom", "leadWidth.nom", 0.22, None),
            ("Lead width min", "leadWidth.min", 0.17, None),
            ("Lead width max", "leadWidth.max", 0.27, None),
            ("Height max", "height.max", 1.6, None),
        ]

    if kind == "qfn":
        return [
            ("Pitch", "pitch", 0.5, None),
            ("Row count", "rowCount", 16, None),
            ("Column count", "columnCount", 16, None),
            ("Body width nom", "bodyWidth.nom", 9.0, None),
            ("Body width min", "bodyWidth.min", 8.9, None),
            ("Body width max", "bodyWidth.max", 9.1, None),
            ("Body length nom", "bodyLength.nom", 9.0, None),
            ("Body length min", "bodyLength.min", 8.9, None),
            ("Body length max", "bodyLength.max", 9.1, None),
            ("Lead length nom", "leadLength.nom", 0.4, None),
            ("Lead length min", "leadLength.min", 0.3, None),
            ("Lead length max", "leadLength.max", 0.5, None),
            ("Lead width nom", "leadWidth.nom", 0.25, None),
            ("Lead width min", "leadWidth.min", 0.18, None),
            ("Lead width max", "leadWidth.max", 0.30, None),
            ("Height max", "height.max", 1.0, None),
            ("Thermal pad width nom", "tabWidth.nom", 0.0, None),
            ("Thermal pad width min", "tabWidth.min", 0.0, None),
            ("Thermal pad width max", "tabWidth.max", 0.0, None),
            ("Thermal pad length nom", "tabLength.nom", 0.0, None),
            ("Thermal pad length min", "tabLength.min", 0.0, None),
            ("Thermal pad length max", "tabLength.max", 0.0, None),
        ]

    if kind == "pqfn":
        return [
            ("Pitch", "pitch", 0.5, None),
            ("Row count", "rowCount", 10, None),
            ("Column count", "columnCount", 10, None),
            ("Body width nom", "bodyWidth.nom", 7.0, None),
            ("Body width min", "bodyWidth.min", 6.9, None),
            ("Body width max", "bodyWidth.max", 7.1, None),
            ("Body length nom", "bodyLength.nom", 7.0, None),
            ("Body length min", "bodyLength.min", 6.9, None),
            ("Body length max", "bodyLength.max", 7.1, None),
            ("Lead length nom", "leadLength.nom", 0.4, None),
            ("Lead length min", "leadLength.min", 0.3, None),
            ("Lead length max", "leadLength.max", 0.5, None),
            ("Lead width nom", "leadWidth.nom", 0.25, None),
            ("Lead width min", "leadWidth.min", 0.18, None),
            ("Lead width max", "leadWidth.max", 0.30, None),
            ("Pull back (nom)", "pullBack.nom", 0.1, None),
            ("Height max", "height.max", 1.0, None),
            ("Thermal pad width nom", "tabWidth.nom", 0.0, None),
            ("Thermal pad width min", "tabWidth.min", 0.0, None),
            ("Thermal pad width max", "tabWidth.max", 0.0, None),
            ("Thermal pad length nom", "tabLength.nom", 0.0, None),
            ("Thermal pad length min", "tabLength.min", 0.0, None),
            ("Thermal pad length max", "tabLength.max", 0.0, None),
        ]

    if kind in ("bga", "cga"):
        return [
            ("Row count", "rowCount", 10, None),
            ("Column count", "columnCount", 10, None),
            ("Pitch", "pitch", 0.8, None),
            ("Lead (ball) diameter nom", "leadDiameter.nom", 0.4, None),
            ("Body width nom", "bodyWidth.nom", 10.0, None),
            ("Body width min", "bodyWidth.min", 9.8, None),
            ("Body width max", "bodyWidth.max", 10.2, None),
            ("Body length nom", "bodyLength.nom", 10.0, None),
            ("Body length min", "bodyLength.min", 9.8, None),
            ("Body length max", "bodyLength.max", 10.2, None),
            ("Height max", "height.max", 1.0, None),
        ]

    if kind == "chip_array":
        return [
            ("Component type", "componentType", "CAPCAV", ["CAPCAV", "INDCAV", "RESCAV", "INDCAF", "RESCAF", "CAPCAF"]),
            ("Lead count", "leadCount", 8, None),
            ("Pitch", "pitch", 0.5, None),
            ("Lead span min", "leadSpan.min", 1.0, None),
            ("Lead span nom", "leadSpan.nom", 1.1, None),
            ("Lead span max", "leadSpan.max", 1.2, None),
            ("Lead length min", "leadLength.min", 0.15, None),
            ("Lead length max", "leadLength.max", 0.25, None),
            ("Lead width min", "leadWidth.min", 0.15, None),
            ("Lead width max", "leadWidth.max", 0.25, None),
            ("Body width nom", "bodyWidth.nom", 2.0, None),
            ("Body length nom", "bodyLength.nom", 1.6, None),
            ("Height max", "height.max", 0.6, None),
            ("Concave ends", "concave", False, None),
            ("Convex E ends", "convex-e", False, None),
            ("Convex S ends", "convex-s", False, None),
            ("Flat ends", "flat", False, None),
        ]

    if kind == "lga":
        return [
            ("Row count", "rowCount", 10, None),
            ("Column count", "columnCount", 10, None),
            ("Horizontal pitch", "horizontalPitch", 0.8, None),
            ("Vertical pitch", "verticalPitch", 0.8, None),
            ("Lead length nom", "leadLength.nom", 0.3, None),
            ("Lead width nom", "leadWidth.nom", 0.3, None),
            ("Body width nom", "bodyWidth.nom", 10.0, None),
            ("Body length nom", "bodyLength.nom", 10.0, None),
            ("Height max", "height.max", 1.0, None),
        ]

    if kind == "melf":
        return [
            ("Body length nom", "bodyLength.nom", 3.2, None),
            ("Body length min", "bodyLength.min", 3.1, None),
            ("Body length max", "bodyLength.max", 3.3, None),
            ("Body diameter nom", "bodyDiameter.nom", 1.6, None),
            ("Body diameter min", "bodyDiameter.min", 1.5, None),
            ("Body diameter max", "bodyDiameter.max", 1.4, None),
            ("Lead length min", "leadLength.min", 0.2, None),
            ("Lead length max", "leadLength.max", 0.5, None),
        ]

    if kind == "cae":
        return (
            [
                ("Lead length nom", "leadLength.nom", 3.4, None),
                ("Lead length min", "leadLength.min", 3.3, None),
                ("Lead length max", "leadLength.max", 3.5, None),
            ]
            + [
                ("Lead width nom", "leadWidth.nom", 1.2, None),
                ("Lead width min", "leadWidth.min", 1.0, None),
                ("Lead width max", "leadWidth.max", 1.4, None),
            ]
            + [("Lead space", "leadSpace.nom", 4.6, None)]
            + [
                ("Body width nom", "bodyWidth.nom", 10.3, None),
                ("Body width min", "bodyWidth.min", 10.1, None),
                ("Body width max", "bodyWidth.max", 10.5, None),
            ]
            + [
                ("Body length nom", "bodyLength.nom", 10.3, None),
                ("Body length min", "bodyLength.min", 10.1, None),
                ("Body length max", "bodyLength.max", 10.5, None),
            ]
            + [
                ("Height max", "height.max", 10.8, None),
                ("Diameter", "bodyDiameter.nom", 10.0, None),
                ("Chamfer", "chamfer", "", None),
            ]
        )

    if kind in ("chip", "crystal", "molded", "sod", "sodfl"):
        base: List[FieldSpec] = []
        if kind == "chip":
            base.append(("Component type", "componentType", "CAPC", ["CAPC", "RESC", "LEDC", "DIOC", "FUSC", "BEADC", "THRMC", "VARC", "INDC"]))
        if kind == "molded":
            base.append(
                (
                    "Component type",
                    "componentType",
                    "capacitor",
                    [
                        "capacitor",
                        "capacitor_polarized",
                        "diode",
                        "diode_non_polarized",
                        "fuse",
                        "inductor",
                        "inductor_precision",
                        "led",
                        "resistor",
                    ],
                )
            )
            base += [
                ("Lead span nom", "leadSpan.nom", 5.075, None),
                ("Lead span min", "leadSpan.min", 4.8, None),
                ("Lead span max", "leadSpan.max", 5.35, None),
            ]
        if kind == "sodfl":
            base += [
                ("Lead span nom", "leadSpan.nom", 5.2, None),
                ("Lead span min", "leadSpan.min", 5.05, None),
                ("Lead span max", "leadSpan.max", 5.35, None),
            ]
        base += [
            ("Body length nom", "bodyLength.nom", 4.25 if kind == "sodfl" else (4.275 if kind == "molded" else 2.0), None),
            ("Body length min", "bodyLength.min", 4.15 if kind == "sodfl" else (3.95 if kind == "molded" else 1.9), None),
            ("Body length max", "bodyLength.max", 4.35 if kind == "sodfl" else (4.6 if kind == "molded" else 2.1), None),
            ("Body width nom", "bodyWidth.nom", 2.6 if kind == "sodfl" else (2.575 if kind == "molded" else 1.25), None),
            ("Body width min", "bodyWidth.min", 2.5 if kind == "sodfl" else (2.25 if kind == "molded" else 1.15), None),
            ("Body width max", "bodyWidth.max", 2.7 if kind == "sodfl" else (2.9 if kind == "molded" else 1.35), None),
            ("Lead length nom", "leadLength.nom", 0.975 if kind == "sodfl" else (1.125 if kind == "molded" else 0.35), None),
            ("Lead length min", "leadLength.min", 0.975 if kind == "sodfl" else (0.75 if kind == "molded" else 0.2), None),
            ("Lead length max", "leadLength.max", 2.025 if kind == "sodfl" else (1.5 if kind == "molded" else 0.5), None),
            ("Height max", "height.max", 1.0 if kind == "sodfl" else (1.05 if kind == "molded" else 1.0), None),
        ]
        if kind == "molded":
            base += [
                ("Lead width nom", "leadWidth.nom", 1.25, None),
                ("Lead width min", "leadWidth.min", 0.95, None),
                ("Lead width max", "leadWidth.max", 1.65, None),
            ]
        if kind == "sodfl":
            base += [
                ("Lead width nom", "leadWidth.nom", 1.35, None),
                ("Lead width min", "leadWidth.min", 1.25, None),
                ("Lead width max", "leadWidth.max", 1.45, None),
            ]
        return base

    if kind == "radial":
        return [
            ("Lead span nom", "leadSpan.nom", 2.54, None),
            ("Lead diameter nom", "leadDiameter.nom", 0.6, None),
            ("Body diameter nom", "bodyDiameter.nom", 5.0, None),
            ("Height max", "height.max", 7.0, None),
        ]

    if kind == "dip":
        return [("Lead count", "leadCount", 8, None), ("Pitch", "pitch", 2.54, None)] + rng(
            "Lead span", "leadSpan.nom", "leadSpan.min", "leadSpan.max"
        ) + [
            ("Lead diameter max", "leadDiameter.max", 0.6, None),
            ("Body length nom", "bodyLength.nom", 10.0, None),
            ("Height nom", "height.nom", 3.0, None),
        ]

    if kind == "mounting_hole":
        return [
            ("Hole diameter", "holeDiameter", 3.2, None),
            ("Pad diameter", "padDiameter", 6.0, None),
        ]

    if kind == "bridge":
        return [
            ("Pad width", "padWidth", 1.0, None),
            ("Pad height", "padHeight", 1.0, None),
        ]

    # Fallback to SOIC schema
    return schema_for_kind("soic")


def _set_nested(obj: Dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    cur: Dict[str, Any] = obj
    for p in parts[:-1]:
        if p not in cur or not isinstance(cur[p], dict):
            cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = value


def element_from_fields(*, kind: str, density: str, name: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build an element dict compatible with the pattern builders.
    """
    settings = copy.deepcopy(DEFAULT_SETTINGS)
    settings["densityLevel"] = (density or "N").strip().upper() or "N"

    # Some builders assume this exists
    try:
        settings["ball"]["collapsible"] = bool(settings.get("ball", {}).get("collapsible", True))
    except Exception:
        pass

    housing: Dict[str, Any] = {"polarized": True}
    for path, val in (fields or {}).items():
        _set_nested(housing, path, val)

    # Oscillator special mapping: variant -> boolean flags
    if kind == "oscillator":
        variant = str(fields.get("variant", "corner-concave")).strip().lower()
        for k in ("corner-concave", "side-concave", "side-flat"):
            housing.pop(k, None)
        if variant in ("corner-concave", "side-concave", "side-flat"):
            housing[variant] = True

    # Force CAE to 2 leads
    if kind == "cae":
        housing["leadCount"] = 2
        housing["cae"] = True

    # Build pins
    lead_count = int(housing.get("leadCount", 0) or 0)
    is_grid = kind in ("bga", "cga", "lga")
    is_quad = kind in ("qfp", "qfn", "pqfn", "cqfp")
    if is_grid and ("rowCount" in housing and "columnCount" in housing):
        row_count = int(housing["rowCount"])
        col_count = int(housing["columnCount"])
        letters = {i: chr(ord("A") + i - 1) for i in range(1, 100)}
        pins = {f"{letters[row]}{col}": {} for row in range(1, row_count + 1) for col in range(1, col_count + 1)}
        lead_count = row_count * col_count
    elif is_quad and ("rowCount" in housing and "columnCount" in housing):
        row_count = int(housing["rowCount"])
        col_count = int(housing["columnCount"])
        lead_count = 2 * (row_count + col_count)
        pins = {str(i): {} for i in range(1, lead_count + 1)}
    else:
        if lead_count <= 0:
            lead_count = 1
        pins = {str(i): {} for i in range(1, lead_count + 1)}

    # For two-pin families, derive leadSpan/leadWidth if absent using body ranges
    two_pin_kinds = {"chip", "crystal", "cae", "melf", "molded", "sod", "sodfl"}
    if kind in two_pin_kinds:
        bw_obj = housing.get("bodyWidth")
        bl_obj = housing.get("bodyLength")
        if "leadWidth" not in housing or not isinstance(housing.get("leadWidth"), dict):
            if isinstance(bw_obj, dict):
                mn = bw_obj.get("min", bw_obj.get("nom", bw_obj.get("max", 0.0)))
                mx = bw_obj.get("max", bw_obj.get("nom", bw_obj.get("min", 0.0)))
                housing["leadWidth"] = {"min": mn, "max": mx}
            elif isinstance(bw_obj, (int, float)):
                housing["leadWidth"] = {"min": float(bw_obj), "max": float(bw_obj)}
        if "leadSpan" not in housing or not isinstance(housing.get("leadSpan"), dict):
            if isinstance(bl_obj, dict):
                nom = bl_obj.get("nom", bl_obj.get("max", bl_obj.get("min", 0.0)))
                mn = bl_obj.get("min", nom)
                mx = bl_obj.get("max", nom)
                housing["leadSpan"] = {"min": mn, "nom": nom, "max": mx}
            elif isinstance(bl_obj, (int, float)):
                housing["leadSpan"] = {"min": float(bl_obj), "nom": float(bl_obj), "max": float(bl_obj)}
        lead_count = max(lead_count, 2)

    # Set polarized flag for chips based on component type
    if kind == "chip":
        comp_type = housing.get("componentType", "CAPC")
        housing["polarized"] = comp_type in ("LEDC", "DIOC")

    element = {
        "name": (name or "").strip(),
        "housing": housing,
        "pins": pins,
        "gridLetters": {i: chr(ord("A") + i - 1) for i in range(1, 100)},
        "library": {"pattern": settings},
    }
    return element


def compute_auto_name(*, kind: str, density: str, name: str, fields: Dict[str, Any]) -> str:
    """
    Compute the auto-generated footprint name for the current inputs.
    """
    element = element_from_fields(kind=kind, density=density, name=name, fields=fields)
    # Avoid generating files; just build to derive name
    pat = build_pattern(kind, element)
    return str(getattr(pat, "name", "") or "")

