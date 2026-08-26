from tools.inspect_gcode_seam import measure_straight_seam


def test_measure_straight_seam_accounts_for_extrusion_width():
    gcode = """
; Z_HEIGHT: 4.2
G1 X127.37 Y148
; FEATURE: Outer wall
; LINE_WIDTH: 0.42
G1 X127.37 Y108 E1.2
G1 X128.63 Y108
; FEATURE: Outer wall
; LINE_WIDTH: 0.42
G1 X128.63 Y148 E1.2
""".splitlines()

    result = measure_straight_seam(
        gcode, seam_x=128.0, y_min=108.0, y_max=148.0,
        min_layer_z=4.2)

    assert result["verified_min_clear_gap_mm"] == 0.84
    assert len(result["measured_layers"]) == 1
