; Process GCode — circle of radius 50mm around center (135,135)
; Repeats on each loop
G90                        ; absolute positioning
G1 X185 Y135 F3000         ; move to start point (center + radius on X)
G2 X185 Y135 I-50 J0 F2000 ; full CW circle, radius 50mm
