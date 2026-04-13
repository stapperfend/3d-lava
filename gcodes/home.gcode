; Homing sequence — moves bed hard to corner (X-,Y-), then centers
; The bed will push against the endstops to find home reliably
G91             ; relative positioning
G1 X-300 Y-300 F3000  ; drive to corner (beyond limit, hits endstops)
G90             ; absolute positioning
G92 X0 Y0      ; define this as machine origin (0,0)
G1 X135 Y135 F3000    ; move to center of working area