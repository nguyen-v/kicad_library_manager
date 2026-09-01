import os


def _dbg(msg: str) -> None:
    """
    Debug logging for footprint generator silkscreen routines.

    IMPORTANT: printing thousands of lines can make KiCad's UI appear hung (stdout can block).
    Enable explicitly via:
      KICAD_FOOTPRINT_GENERATOR_DEBUG=1
    """
    try:
        v = str(os.environ.get("KICAD_FOOTPRINT_GENERATOR_DEBUG", "") or "").strip().lower()
    except Exception:
        v = ""
    if v in ("1", "true", "yes", "on"):
        try:
            print(msg)
        except Exception:
            pass


def preamble(pattern, housing):
    line_width = pattern.settings['lineWidth']['silkscreen']
    
    # Calculate text position - above the component using real pad positions
    if 'bodyWidth' in housing and 'bodyLength' in housing:
        body_y = housing['bodyLength']['nom'] / 2
        
        # Find the actual maximum pad extent
        pad_extent = 0
        if pattern.pads:
            for pad in pattern.pads.values():
                # Calculate the farthest point of each pad from center
                pad_top = pad.y + pad.height / 2
                pad_extent = max(pad_extent, abs(pad_top))
        
        if pad_extent == 0:
            pad_extent = 0.7  # fallback if no pads found
        
        courtyard = pattern.settings.get('clearance', {}).get('courtyard', 0.25)
        text_y = -(max(body_y, pad_extent) + 1.25)
    else:
        text_y = -1.5  # fallback
    
    pattern.layer('topSilkscreen').lineWidth(line_width).attribute(
        'refDes',
        {
            'x': 0,
            'y': text_y,
            'halign': 'center',
            'valign': 'center',
        },
    )
    if 'silkscreen' in housing and housing['silkscreen']:
        # custom path support omitted for brevity
        pass
    return pattern


def chip_preamble(pattern, housing):
    """Preamble for chip components with refDes positioning following QFP-like logic"""
    line_width = pattern.settings['lineWidth']['silkscreen']
    
    # Calculate text position using real pad positions (similar to QFP)
    if pattern.pads:
        pad_extent = 0
        for pad in pattern.pads.values():
            # Calculate the farthest point of each pad from center
            # For chip (horizontal layout), use Y extent
            pad_extent = max(pad_extent, abs(pad.y) + pad.height / 2)
        
        if pad_extent == 0:
            pad_extent = 0.7  # fallback if no pads found
        
        # Use chip body dimensions for comparison
        body_y = housing['bodyWidth']['nom'] / 2  # For chip, body width becomes Y extent
        courtyard = pattern.settings.get('clearance', {}).get('courtyard', 0.25)
        text_y = -(max(body_y, pad_extent) + 1.25)
    else:
        text_y = -1.5  # fallback
    
    pattern.layer('topSilkscreen').lineWidth(line_width).attribute(
        'refDes',
        {
            'x': 0,
            'y': text_y,
            'halign': 'center',
            'valign': 'center',
        },
    )
    if 'silkscreen' in housing and housing['silkscreen']:
        # custom path support omitted for brevity
        pass
    return pattern


def body(pattern, housing):
    w = housing['bodyWidth']['nom']
    l = housing['bodyLength']['nom']
    lw = pattern.settings['lineWidth']['silkscreen']
    x = w / 2 + lw / 2
    y = l / 2 + lw / 2
    preamble(pattern, housing).rectangle(-x, -y, x, y)


def dual(pattern, housing):
    s = pattern.settings
    lw = s['lineWidth']['silkscreen']
    w = housing['bodyWidth']['nom']
    l = housing['bodyLength']['nom']
    first_pad = list(pattern.pads.values())[0]
    gap = lw / 2 + s['clearance']['padToSilk']
    x1 = -w / 2 - lw / 2
    x2 = -x1
    yb = -l / 2 - lw / 2
    xf = first_pad.x - first_pad.width / 2 - gap
    yf = first_pad.y - first_pad.height / 2 - gap
    y1 = min(yb, yf)
    y2 = -y1
    xp = first_pad.x
    yp = (yf if xp < x1 else y1) - 1.5 * lw
    preamble(pattern, housing)
    
    if housing.get('son') or housing.get('sot23') or housing.get('sop') or housing.get('soj') or housing.get('soic'):
        # SON/SOT-23/SOP/SOIC-specific: only draw horizontal lines (top and bottom)
        # Lines should be close to body edges, not based on pad positions
        body_left = -w / 2
        body_right = w / 2
        # Use body-relative positioning with small offset (like SOP)
        body_y1 = -l / 2 - lw / 2  # bottom line (body bottom edge - line width)
        body_y2 = l / 2 + lw / 2   # top line (body top edge + line width)
        pattern.line(body_left, body_y1, body_right, body_y1)  # bottom horizontal line
        pattern.line(body_left, body_y2, body_right, body_y2)  # top horizontal line
        
        # Add pin 1 indicator similar to QFP
        if housing.get('polarized'):
            # Get first pad and silk clearance (same as QFP)
            pad1 = list(pattern.pads.values())[0]
            pad1_x = pad1.x
            pad1_y = pad1.y
            pad1_size_y = pad1.height
            silk_to_pad_clearance = s['clearance']['silkToPad']
            
            # Y position: same calculation as QFP
            dot1_y = pad1_y - pad1_size_y/2 - 0.25 - silk_to_pad_clearance
            
            # X position: align with the top-left pad x position
            if housing.get('sot23') or housing.get('sop') or housing.get('soj') or housing.get('soic'):
                dot1_x = pad1_x  # Aligned with the first pad's X position
            else:
                dot1_x = body_left - 0.25 - silk_to_pad_clearance
            
            # Check for collision with silkscreen lines and maintain minimum silk-to-silk distance
            min_silk_distance = 0.2  # 0.2mm minimum silk-to-silk distance
            dot_radius = 0.2    # dot radius
            dot_line_width = 0.1  # dot line width
            silk_line_width = lw  # silkscreen line width (typically 0.12mm)
            
            # Calculate required clearance from dot center to line center
            # This accounts for: dot radius + dot line width/2 + min clearance + silk line width/2
            dot_outer_radius = dot_radius + dot_line_width / 2
            required_clearance = dot_outer_radius + min_silk_distance + silk_line_width / 2
            
            # Check distance from dot center to line centers (top and bottom)
            distance_to_top = abs(dot1_y - body_y2)
            distance_to_bottom = abs(dot1_y - body_y1)
            
            # If too close to horizontal lines, move dot left to maintain clearance
            if distance_to_top < required_clearance or distance_to_bottom < required_clearance:
                # Move dot left by the amount needed to clear the minimum distance
                # Consider the closest horizontal line
                closest_line_distance = min(distance_to_top, distance_to_bottom)
                if closest_line_distance < required_clearance:
                    # Calculate how much to move left to achieve required_clearance
                    move_distance = required_clearance - closest_line_distance
                    dot1_x = pad1_x - move_distance
            
            # Ensure dot doesn't go too far left (stay reasonable relative to pad)
            max_left_offset = pad1_x - 1.0  # Don't move more than 1mm left from pad center
            if dot1_x < max_left_offset:
                dot1_x = max_left_offset
            
            # Circle with 0.2mm radius, 0.1mm line width, filled
            pattern.layer('topSilkscreen').lineWidth(0.1).fill(True).circle(dot1_x, dot1_y, 0.2).fill(False)
    else:
        # Standard dual: draw full rectangle
        pattern.rectangle(x1, y1, x2, y2)
    
    if housing.get('polarized') and not housing.get('son') and not housing.get('sot23') and not housing.get('sop') and not housing.get('soj') and not housing.get('soic'):
        pattern.attribute('value', {'text': pattern.name, 'x': 0, 'y': 0})
        pattern.circle(xp, yp, 0)  # polarityMark abstraction skipped; use small dot if needed


def corner_concave(pattern, housing):
    """Silkscreen for corner concave oscillator: no rectangle, lines between pads on four sides, SON-style dot"""
    s = pattern.settings
    lw = s['lineWidth']['silkscreen']
    w = housing['bodyWidth']['nom']
    l = housing['bodyLength']['nom']
    
    silk_pad_clearance = s['clearance']['silkToPad']
    silk_line_width = s['lineWidth']['silkscreen']
    silk_pad_offset = silk_pad_clearance + silk_line_width / 2
    
    # For corner concave oscillator: draw lines between pads on all four sides
    # Get all 4 pads (corner concave has exactly 4 pads)
    pads = list(pattern.pads.values())
    
    if len(pads) >= 4:
        # Find pads by position (assuming standard corner concave layout)
        # Pad 1: bottom-left, Pad 3: top-left, Pad 4: top-right, Pad 2: bottom-right
        pad_positions = {}
        for pad in pads:
            if pad.x < 0 and pad.y < 0:  # bottom-left
                pad_positions['bottom_left'] = pad
            elif pad.x < 0 and pad.y > 0:  # top-left  
                pad_positions['top_left'] = pad
            elif pad.x > 0 and pad.y > 0:  # top-right
                pad_positions['top_right'] = pad
            elif pad.x > 0 and pad.y < 0:  # bottom-right
                pad_positions['bottom_right'] = pad
        
        preamble(pattern, housing)
        
        # Draw horizontal lines (top and bottom) - positioned outside body edges like QFP/QFN
        body_top = l / 2 + lw / 2
        body_bottom = -l / 2 - lw / 2
        
        # Top line: outside body top edge
        if 'top_left' in pad_positions and 'top_right' in pad_positions:
            left_pad = pad_positions['top_left']
            right_pad = pad_positions['top_right']
            line_start_x = left_pad.x + left_pad.width/2 + silk_pad_offset
            line_end_x = right_pad.x - right_pad.width/2 - silk_pad_offset
            if line_end_x > line_start_x:
                pattern.line(line_start_x, body_top, line_end_x, body_top)
        
        # Bottom line: outside body bottom edge
        if 'bottom_left' in pad_positions and 'bottom_right' in pad_positions:
            left_pad = pad_positions['bottom_left']
            right_pad = pad_positions['bottom_right']
            line_start_x = left_pad.x + left_pad.width/2 + silk_pad_offset
            line_end_x = right_pad.x - right_pad.width/2 - silk_pad_offset
            if line_end_x > line_start_x:
                pattern.line(line_start_x, body_bottom, line_end_x, body_bottom)
        
        # Draw vertical lines (left and right) - positioned outside body edges like QFP/QFN
        body_left = -w / 2 - lw / 2
        body_right = w / 2 + lw / 2
        
        # Left line: outside body left edge
        if 'bottom_left' in pad_positions and 'top_left' in pad_positions:
            bottom_pad = pad_positions['bottom_left']
            top_pad = pad_positions['top_left']
            line_start_y = bottom_pad.y + bottom_pad.height/2 + silk_pad_offset
            line_end_y = top_pad.y - top_pad.height/2 - silk_pad_offset
            if line_end_y > line_start_y:
                pattern.line(body_left, line_start_y, body_left, line_end_y)
        
        # Right line: outside body right edge
        if 'bottom_right' in pad_positions and 'top_right' in pad_positions:
            bottom_pad = pad_positions['bottom_right']
            top_pad = pad_positions['top_right']
            line_start_y = bottom_pad.y + bottom_pad.height/2 + silk_pad_offset
            line_end_y = top_pad.y - top_pad.height/2 - silk_pad_offset
            if line_end_y > line_start_y:
                pattern.line(body_right, line_start_y, body_right, line_end_y)
        
        # Add pin 1 dot indicator (1mm away from pad)
        if housing.get('polarized') and 'top_left' in pad_positions:
            pad1 = pad_positions['top_left']  # Pin 1 is top-left (with [4,1,3,2] ordering)
            # Position 1mm away from pad edge
            dot1_x = pad1.x - pad1.width/2 - 0.5  # 1mm left from pad edge
            dot1_y = pad1.y  # Aligned with pad 1 Y position
            # Circle with 0.2mm radius, 0.1mm line width, filled
            pattern.layer('topSilkscreen').lineWidth(0.1).fill(True).circle(dot1_x, dot1_y, 0.2).fill(False)


def grid_array(pattern, housing):
    s = pattern.settings
    lw = s['lineWidth']['silkscreen']
    w = housing['bodyWidth']['nom']
    l = housing['bodyLength']['nom']
    x = w / 2 + lw / 2
    y = l / 2 + lw / 2
    dx = x - housing['horizontalPitch'] * (housing['columnCount'] / 2 - 0.5)
    dy = y - housing['verticalPitch'] * (housing['rowCount'] / 2 - 0.5)
    d = min(dx, dy)
    length = min(2 * housing['horizontalPitch'], 2 * housing['verticalPitch'], x, y)
    p = preamble(pattern, housing)
    p.moveTo(-x, -y + length).lineTo(-x, -y + d).lineTo(-x + d, -y).lineTo(-x + length, -y)
    p.moveTo(x, -y + length).lineTo(x, -y).lineTo(x - length, -y)
    p.moveTo(x, y - length).lineTo(x, y).lineTo(x - length, y)
    p.moveTo(-x, y - length).lineTo(-x, y).lineTo(-x + length, y)
    # Add pin-1 marker (dot) like QFN: top-left corner, inset from edges.
    try:
        if housing.get('polarized'):
            dot_offset = 0.8
            dot_x = -x + dot_offset
            dot_y = -y + dot_offset
            pattern.layer('topSilkscreen').lineWidth(0.1).fill(True).circle(dot_x, dot_y, 0.2).fill(False)
    except Exception:
        pass


def pak(pattern, housing):
    s = pattern.settings
    lw = s['lineWidth']['silkscreen']
    bw = housing.get('bodyWidth', {}).get('nom', housing.get('bodyWidth', {}).get('max'))
    bl = housing.get('bodyLength', {}).get('nom', housing.get('bodyLength', {}).get('max'))
    ls = housing['leadSpan']['nom']
    tab = housing.get('tabLedge')
    if isinstance(tab, dict):
        tab_ledge = tab.get('nom', tab.get('min', tab.get('max', 0)))
    else:
        tab_ledge = tab if tab is not None else 0
    first_pad, last_pad = list(pattern.pads.values())[0], list(pattern.pads.values())[-1]
    gap = lw / 2 + s['clearance']['padToSilk']
    dx = ls / 2 - tab_ledge - bw / 2
    x1 = dx - bw / 2 - lw / 2
    x2 = dx + bw / 2 + lw / 2
    y1 = -bl / 2 - lw / 2
    y2 = -y1
    xf = first_pad.x - first_pad.width / 2 - gap
    yf = first_pad.y - first_pad.height / 2 - gap
    xt = last_pad.x - last_pad.width / 2 - gap
    yt = last_pad.y - last_pad.height / 2 - gap
    xp = first_pad.x
    yp = (yf if xp < x1 else y1) - 1.5 * lw
    preamble(pattern, housing)
    pattern.silk_rectangle(x1, y1, x2, y2) if hasattr(pattern, 'silk_rectangle') else pattern.rectangle(x1, y1, x2, y2)
    pattern.circle(xp, yp, 0)
    pattern.moveTo(x1, yf).lineTo(xf, yf).lineTo(xf, yf + first_pad.height + gap)
    if yt < y1:
        pattern.moveTo(x2, yt).lineTo(xt, yt).lineTo(xt, y1)
        pattern.moveTo(x2, -yt).lineTo(xt, -yt).lineTo(xt, -y1)

def quad(pattern, housing):
    s = pattern.settings
    silk_line_width = s['lineWidth']['silkscreen']
    silk_pad_clearance = s['clearance']['silkToPad']
    silk_pad_offset = silk_pad_clearance + silk_line_width / 2
    
    body_width = housing['bodyWidth']['nom']
    body_length = housing['bodyLength']['nom']
    
    # Debug: Identify package type (guarded; see _dbg()).
    package_type = "QFP" if housing.get('qfp') else ("QFN" if housing.get('qfn') else "CQFP")
    _dbg(f"DEBUG QUAD SILKSCREEN: Package type = {package_type}")
    _dbg(f"DEBUG: Body dimensions = {body_width:.3f} x {body_length:.3f}")
    _dbg(f"DEBUG: Pad count = {len(list(pattern.pads.values()))}")
    
    # Get all pads to find clearance boundaries
    pads = list(pattern.pads.values())
    
    # Calculate body outline position (just outside the actual body)
    silk_fab_offset = 0.1  # small offset from body edge
    body_x = body_width / 2 + silk_fab_offset
    body_y = body_length / 2 + silk_fab_offset
    
    # Calculate corner line lengths based on available space around corners
    # Find the corner pads (closest to each corner) to determine constraints
    
    # Find pads in each quadrant closest to corners
    corner_pads = {
        'top_left': None, 'top_right': None, 
        'bottom_left': None, 'bottom_right': None
    }
    
    for pad in pads:
        # Determine which corner this pad is closest to
        if pad.x < 0 and pad.y > 0:  # Top-left quadrant
            if (corner_pads['top_left'] is None or 
                (abs(pad.x) + pad.y) < (abs(corner_pads['top_left'].x) + corner_pads['top_left'].y)):
                corner_pads['top_left'] = pad
        elif pad.x > 0 and pad.y > 0:  # Top-right quadrant
            if (corner_pads['top_right'] is None or 
                (pad.x + pad.y) < (corner_pads['top_right'].x + corner_pads['top_right'].y)):
                corner_pads['top_right'] = pad
        elif pad.x < 0 and pad.y < 0:  # Bottom-left quadrant
            if (corner_pads['bottom_left'] is None or 
                (abs(pad.x) + abs(pad.y)) < (abs(corner_pads['bottom_left'].x) + abs(corner_pads['bottom_left'].y))):
                corner_pads['bottom_left'] = pad
        elif pad.x > 0 and pad.y < 0:  # Bottom-right quadrant
            if (corner_pads['bottom_right'] is None or 
                (pad.x + abs(pad.y)) < (corner_pads['bottom_right'].x + abs(corner_pads['bottom_right'].y))):
                corner_pads['bottom_right'] = pad
    
    # Calculate corner line lengths to maintain exact clearance to nearest pads
    # Find the maximum length that maintains silk_pad_clearance from line edge to pad edge
    
    max_corner_length_x = float('inf')
    max_corner_length_y = float('inf')
    
    _dbg(f"DEBUG: Body position: body_x=±{body_x:.3f}, body_y=±{body_y:.3f}")
    _dbg(f"DEBUG: Required clearance: {silk_pad_clearance:.3f}, line width: {silk_line_width:.3f}")
    
    # Better logic for QFN: identify pads by position patterns
    # Group pads by their approximate positions to find edge pads
    top_pads = []
    bottom_pads = []
    left_pads = []
    right_pads = []
    
    # Find the extreme Y positions (top/bottom edge pads)
    max_y = max(pad.y for pad in pads)
    min_y = min(pad.y for pad in pads)
    max_x = max(pad.x for pad in pads)
    min_x = min(pad.x for pad in pads)
    
    _dbg(f"DEBUG: Pad position ranges: Y from {min_y:.3f} to {max_y:.3f}, X from {min_x:.3f} to {max_x:.3f}")
    
    # Tolerance for grouping pads (within 0.1mm of edge)
    tolerance = 0.1
    
    for i, pad in enumerate(pads):
        _dbg(f"DEBUG: Analyzing pad {i}: pos=({pad.x:.3f}, {pad.y:.3f}), size={pad.width:.3f}x{pad.height:.3f}")
        
        is_top = abs(pad.y - max_y) < tolerance
        is_bottom = abs(pad.y - min_y) < tolerance  
        is_left = abs(pad.x - min_x) < tolerance
        is_right = abs(pad.x - max_x) < tolerance
        
        _dbg(f"  Top?{is_top}, Bottom?{is_bottom}, Left?{is_left}, Right?{is_right}")
        
        if is_top:
            top_pads.append(pad)
        elif is_bottom:
            bottom_pads.append(pad)
        elif is_left:
            left_pads.append(pad)
        elif is_right:
            right_pads.append(pad)
    
    _dbg(f"DEBUG: Found {len(top_pads)} top, {len(bottom_pads)} bottom, {len(left_pads)} left, {len(right_pads)} right pads")
    
    # Process top/bottom pads for horizontal constraints
    for pads_group, group_name in [(top_pads + bottom_pads, "top/bottom")]:
        for i, pad in enumerate(pads_group):
            _dbg(f"DEBUG: Processing {group_name} pad {i}: pos=({pad.x:.3f}, {pad.y:.3f})")
            # For horizontal corner lines: find constraint from pads on top/bottom
            # Need the OUTER edge of the pad (farthest from body center)
            pad_edge_x = abs(pad.x) + pad.width/2  # outer edge distance from center
            
            _dbg(f"DEBUG: Pad {i} (top/bottom): pos=({pad.x:.3f}, {pad.y:.3f}), size={pad.width:.3f}x{pad.height:.3f}")
            _dbg(f"  pad_edge_x (outer) = {pad_edge_x:.3f}")
            
            # Corner line starts at body_x and extends inward toward center
            # For max allowable length: corner_length_x = body_x - silk_line_width - silk_pad_clearance - pad_edge_x
            max_length = body_x - silk_line_width - silk_pad_clearance - pad_edge_x
            
            _dbg(f"  calculation: {body_x:.3f} - {silk_line_width:.3f} - {silk_pad_clearance:.3f} - {pad_edge_x:.3f} = {max_length:.3f}")
            
            if max_length > 0:
                max_corner_length_x = min(max_corner_length_x, max_length)
                _dbg(f"  updated max_corner_length_x = {max_corner_length_x:.3f}")
    
    # Process left/right pads for vertical constraints  
    for pads_group, group_name in [(left_pads + right_pads, "left/right")]:
        for i, pad in enumerate(pads_group):
            _dbg(f"DEBUG: Processing {group_name} pad {i}: pos=({pad.x:.3f}, {pad.y:.3f})")
            # For vertical corner lines: find constraint from pads on left/right
            # Need the OUTER edge of the pad (farthest from body center)
            pad_edge_y = abs(pad.y) + pad.height/2  # outer edge distance from center
            
            _dbg(f"DEBUG: Pad {i} (left/right): pos=({pad.x:.3f}, {pad.y:.3f}), size={pad.width:.3f}x{pad.height:.3f}")
            _dbg(f"  pad_edge_y (outer) = {pad_edge_y:.3f}")
            
            # Corner line starts at body_y and extends inward toward center
            # For max allowable length: corner_length_y = body_y - silk_line_width - silk_pad_clearance - pad_edge_y
            max_length = body_y - silk_line_width - silk_pad_clearance - pad_edge_y
            
            _dbg(f"  calculation: {body_y:.3f} - {silk_line_width:.3f} - {silk_pad_clearance:.3f} - {pad_edge_y:.3f} = {max_length:.3f}")
            
            if max_length > 0:
                max_corner_length_y = min(max_corner_length_y, max_length)
                _dbg(f"  updated max_corner_length_y = {max_corner_length_y:.3f}")
    
    # Use the calculated maximum lengths, with reasonable defaults if no constraints
    corner_length_x = max_corner_length_x if max_corner_length_x != float('inf') else body_width * 0.15
    corner_length_y = max_corner_length_y if max_corner_length_y != float('inf') else body_length * 0.15
    
    # Apply maximum size limit (don't make lines longer than 30% of body)
    corner_length_x = min(corner_length_x, body_width * 0.3)
    corner_length_y = min(corner_length_y, body_length * 0.3)
    
    _dbg(f"Calculated corner lengths (exact clearance): x={corner_length_x:.3f}, y={corner_length_y:.3f}")
    _dbg(f"After 30% body constraint: x={corner_length_x:.3f}, y={corner_length_y:.3f}")
    
    # Ensure minimum line length
    min_line_length = 0.2
    corner_length_x = max(corner_length_x, min_line_length)
    corner_length_y = max(corner_length_y, min_line_length)
    
    preamble(pattern, housing)
    
    # Draw corner markers at each corner of the body
    # L-shaped markers pointing TOWARD the body center
    
    _dbg(f"DEBUG: Drawing lines with lengths x={corner_length_x:.3f}, y={corner_length_y:.3f}")
    
    # Calculate actual line end positions and verify clearances
    line_end_x = body_x - corner_length_x
    line_end_y = body_y - corner_length_y
    line_edge_x = line_end_x - silk_line_width/2  # edge of line considering thickness
    line_edge_y = line_end_y - silk_line_width/2
    
    _dbg(f"DEBUG: Line ends at: x={line_end_x:.3f}, y={line_end_y:.3f}")
    _dbg(f"DEBUG: Line edges at: x={line_edge_x:.3f}, y={line_edge_y:.3f}")
    
    # Find nearest pads to verify clearance
    nearest_pad_x = None
    nearest_pad_y = None
    min_dist_x = float('inf')
    min_dist_y = float('inf')
    
    for pad in pads:
        if abs(pad.y) > body_length / 2:  # Top/bottom pad
            pad_edge = abs(pad.x) - pad.width/2
            dist = pad_edge - line_edge_x
            if dist < min_dist_x:
                min_dist_x = dist
                nearest_pad_x = pad
        
        if abs(pad.x) > body_width / 2:  # Left/right pad
            pad_edge = abs(pad.y) - pad.height/2
            dist = pad_edge - line_edge_y
            if dist < min_dist_y:
                min_dist_y = dist
                nearest_pad_y = pad
    
    if nearest_pad_x:
        _dbg(f"DEBUG: Nearest X-constraining pad: pos=({nearest_pad_x.x:.3f}, {nearest_pad_x.y:.3f}), clearance={min_dist_x:.3f}")
    if nearest_pad_y:
        _dbg(f"DEBUG: Nearest Y-constraining pad: pos=({nearest_pad_y.x:.3f}, {nearest_pad_y.y:.3f}), clearance={min_dist_y:.3f}")
    
    # Top-left corner
    pattern.line(-body_x, body_y, -body_x + corner_length_x, body_y)   # horizontal (toward right)
    pattern.line(-body_x, body_y, -body_x, body_y - corner_length_y)   # vertical (toward down)
    
    # Top-right corner  
    pattern.line(body_x, body_y, body_x - corner_length_x, body_y)     # horizontal (toward left)
    pattern.line(body_x, body_y, body_x, body_y - corner_length_y)     # vertical (toward down)
    
    # Bottom-right corner
    pattern.line(body_x, -body_y, body_x - corner_length_x, -body_y)   # horizontal (toward left)
    pattern.line(body_x, -body_y, body_x, -body_y + corner_length_y)   # vertical (toward up)
    
    # Bottom-left corner
    pattern.line(-body_x, -body_y, -body_x + corner_length_x, -body_y) # horizontal (toward right)
    pattern.line(-body_x, -body_y, -body_x, -body_y + corner_length_y) # vertical (toward up)
    
    # Add polarity marker if needed
    if housing.get('polarized'):
        # Place dot1 circle above the first pad
        pad1 = list(pattern.pads.values())[0]
        pad1_x = pad1.x
        pad1_y = pad1.y
        pad1_size_y = pad1.height
        silk_to_pad_clearance = s['clearance']['silkToPad']
        
        dot1_x = pad1_x - 0.75  # Move 0.75mm to the left
        dot1_y = pad1_y - pad1_size_y/2 - 0.25 - silk_to_pad_clearance
        
        # Circle with 0.2mm radius, 0.1mm line width, filled
        pattern.layer('topSilkscreen').lineWidth(0.1).fill(True).circle(dot1_x, dot1_y, 0.2).fill(False)


def sodfl_preamble(pattern, housing):
    """Preamble for SODFL components with proper refDes positioning"""
    line_width = pattern.settings['lineWidth']['silkscreen']
    
    # Calculate text position using real pad positions (similar to chip)
    if pattern.pads:
        pad_extent = 0
        for pad in pattern.pads.values():
            # Calculate the farthest point of each pad from center
            # For SODFL (horizontal layout), use Y extent
            pad_extent = max(pad_extent, abs(pad.y) + pad.height / 2)
        
        if pad_extent == 0:
            pad_extent = 0.7  # fallback if no pads found
        
        # Use SODFL body dimensions for comparison
        body_y = housing['bodyWidth']['nom'] / 2  # For SODFL, body width becomes Y extent
        courtyard = pattern.settings.get('clearance', {}).get('courtyard', 0.25)
        text_y = -(max(body_y, pad_extent) + 1.25)
    else:
        text_y = -1.5  # fallback
    
    pattern.layer('topSilkscreen').lineWidth(line_width).attribute(
        'refDes',
        {
            'x': 0,
            'y': text_y,
            'halign': 'center',
            'valign': 'center',
        },
    )
    if 'silkscreen' in housing and housing['silkscreen']:
        # custom path support omitted for brevity
        pass
    return pattern


def sodfl(pattern, housing):
    """SODFL-specific silkscreen with U-shaped outline encircling body and larger polarity dot"""
    s = pattern.settings
    lw = s['lineWidth']['silkscreen']
    silk_pad_clearance = s['clearance']['silkToPad']
    
    # Get body dimensions (using chip coordinates: swapped x/y due to 90° rotation)
    bw = housing['bodyWidth']['nom']  # This becomes Y extent
    bl = housing['bodyLength']['nom']  # This becomes X extent
    
    # Since SODFL is rotated like chip: x = bl/2, y = bw/2
    body_x = bl / 2  # Use body length for X extent
    body_y = bw / 2  # Use body width for Y extent
    
    # Body outline coordinates (with line width offset)
    body_left = -body_x - lw / 2
    body_right = body_x + lw / 2
    body_top = body_y + lw / 2
    body_bottom = -body_y - lw / 2
    
    # Get pad information
    pads = list(pattern.pads.values())
    if len(pads) >= 2:
        pad1 = pads[0]  # Left pad
        pad2 = pads[1]  # Right pad
        
        # Calculate pad clearance boundaries (0.2mm clearance from pad edges)
        pad_clearance = 0.2  # As requested
        pad1_right = pad1.x + pad1.width / 2 + pad_clearance
        pad2_left = pad2.x - pad2.width / 2 - pad_clearance
        pad_top = max(abs(pad1.y) + pad1.height / 2, abs(pad2.y) + pad2.height / 2) + pad_clearance
        pad_bottom = -pad_top
        
        # Use SODFL-specific preamble for text positioning
        sodfl_preamble(pattern, housing)
        
        # Draw U-shaped silkscreen: top U and bottom U (not left and right)
        # Account for line width to maintain proper clearance to pads
        
        # Top U (encircling top of body) - shaped like ∩ 
        # Vertical lines need to stop before reaching pad clearance + line width/2
        top_vertical_end = pad_top + lw / 2  # Account for line thickness
        pattern.line(body_left, body_top, body_left, top_vertical_end)   # left vertical
        pattern.line(body_left, body_top, body_right, body_top)          # top horizontal  
        pattern.line(body_right, body_top, body_right, top_vertical_end) # right vertical
        
        # Bottom U (encircling bottom of body) - shaped like ∪
        # Vertical lines need to stop before reaching pad clearance + line width/2
        bottom_vertical_end = pad_bottom - lw / 2  # Account for line thickness
        pattern.line(body_left, body_bottom, body_left, bottom_vertical_end)  # left vertical
        pattern.line(body_left, body_bottom, body_right, body_bottom)         # bottom horizontal
        pattern.line(body_right, body_bottom, body_right, bottom_vertical_end) # right vertical
        
        # Add larger polarity dot (0.5mm as requested, moved 0.1mm more to the left)
        if housing.get('polarized'):
            # Position relative to left pad (pin 1), moved 0.1mm more to the left
            dot_x = pad1.x - pad1.width / 2 - silk_pad_clearance - 0.6  # Original offset + 0.1mm more
            dot_y = 0  # Centered vertically
            
            # Draw dot with 0.2mm radius, 0.1mm line width, filled
            pattern.layer('topSilkscreen').lineWidth(0.1).fill(True).circle(dot_x, dot_y, 0.2).fill(False)
    else:
        # Fallback: use SODFL preamble for text positioning
        sodfl_preamble(pattern, housing)


def molded_preamble(pattern, housing):
    """Preamble for molded components with proper refDes positioning"""
    line_width = pattern.settings['lineWidth']['silkscreen']
    
    # Calculate text position using real pad positions (similar to SODFL)
    if pattern.pads:
        pad_extent = 0
        for pad in pattern.pads.values():
            # Calculate the farthest point of each pad from center
            # For molded (horizontal layout), use Y extent
            pad_extent = max(pad_extent, abs(pad.y) + pad.height / 2)
        
        if pad_extent == 0:
            pad_extent = 0.7  # fallback if no pads found
        
        # Use molded body dimensions for comparison
        body_y = housing['bodyWidth']['nom'] / 2  # For molded, body width becomes Y extent
        courtyard = pattern.settings.get('clearance', {}).get('courtyard', 0.25)
        text_y = -(max(body_y, pad_extent) + 1.25)
    else:
        text_y = -1.5  # fallback
    
    pattern.layer('topSilkscreen').lineWidth(line_width).attribute(
        'refDes',
        {
            'x': 0,
            'y': text_y,
            'halign': 'center',
            'valign': 'center',
        },
    )
    if 'silkscreen' in housing and housing['silkscreen']:
        # custom path support omitted for brevity
        pass
    return pattern


def molded(pattern, housing):
    """Molded-specific silkscreen with U-shaped outline encircling body and larger polarity dot (same as SODFL)"""
    s = pattern.settings
    lw = s['lineWidth']['silkscreen']
    silk_pad_clearance = s['clearance']['silkToPad']
    
    # Get body dimensions (using chip coordinates: swapped x/y due to 90° rotation)
    bw = housing['bodyWidth']['nom']  # This becomes Y extent
    bl = housing['bodyLength']['nom']  # This becomes X extent
    
    # Since molded is rotated like chip: x = bl/2, y = bw/2
    body_x = bl / 2  # Use body length for X extent
    body_y = bw / 2  # Use body width for Y extent
    
    # Body outline coordinates (with line width offset)
    body_left = -body_x - lw / 2
    body_right = body_x + lw / 2
    body_top = body_y + lw / 2
    body_bottom = -body_y - lw / 2
    
    # Get pad information
    pads = list(pattern.pads.values())
    if len(pads) >= 2:
        pad1 = pads[0]  # Left pad
        pad2 = pads[1]  # Right pad
        
        # Calculate pad clearance boundaries (0.2mm clearance from pad edges)
        pad_clearance = 0.2  # As requested
        pad1_right = pad1.x + pad1.width / 2 + pad_clearance
        pad2_left = pad2.x - pad2.width / 2 - pad_clearance
        pad_top = max(abs(pad1.y) + pad1.height / 2, abs(pad2.y) + pad2.height / 2) + pad_clearance
        pad_bottom = -pad_top
        
        # Use molded-specific preamble for text positioning
        molded_preamble(pattern, housing)
        
        # Draw U-shaped silkscreen: top U and bottom U (same as SODFL)
        # Account for line width to maintain proper clearance to pads
        
        # Top U (encircling top of body) - shaped like ∩ 
        # Vertical lines need to stop before reaching pad clearance + line width/2
        top_vertical_end = pad_top + lw / 2  # Account for line thickness
        pattern.line(body_left, body_top, body_left, top_vertical_end)   # left vertical
        pattern.line(body_left, body_top, body_right, body_top)          # top horizontal  
        pattern.line(body_right, body_top, body_right, top_vertical_end) # right vertical
        
        # Bottom U (encircling bottom of body) - shaped like ∪
        # Vertical lines need to stop before reaching pad clearance + line width/2
        bottom_vertical_end = pad_bottom - lw / 2  # Account for line thickness
        pattern.line(body_left, body_bottom, body_left, bottom_vertical_end)  # left vertical
        pattern.line(body_left, body_bottom, body_right, body_bottom)         # bottom horizontal
        pattern.line(body_right, body_bottom, body_right, bottom_vertical_end) # right vertical
        
        # Add larger polarity dot (0.5mm as requested, moved 0.1mm more to the left)
        if housing.get('polarized'):
            # Position relative to left pad (pin 1), moved 0.1mm more to the left
            dot_x = pad1.x - pad1.width / 2 - silk_pad_clearance - 0.6  # Original offset + 0.1mm more
            dot_y = 0  # Centered vertically
            
            # Draw dot with 0.2mm radius, 0.1mm line width, filled
            pattern.layer('topSilkscreen').lineWidth(0.1).fill(True).circle(dot_x, dot_y, 0.2).fill(False)
    else:
        # Fallback: use molded preamble for text positioning
        molded_preamble(pattern, housing)


def dfn_molded_style(pattern, housing):
    """DFN-specific silkscreen using molded-style U-shapes (top and bottom), works for 2/3/4 pins.
    Computes pad extents from all pads and does not assume pads[0] is left and pads[1] is right."""
    s = pattern.settings
    lw = s['lineWidth']['silkscreen']
    silk_pad_clearance = s['clearance']['silkToPad']

    # Body dimensions (DFN horizontal layout similar to molded: x = bl/2, y = bw/2)
    bw = housing['bodyWidth']['nom']
    bl = housing['bodyLength']['nom']
    body_x = bl / 2
    body_y = bw / 2

    # Body outline coordinates (with line width offset)
    body_left = -body_x - lw / 2
    body_right = body_x + lw / 2
    body_top = body_y + lw / 2
    body_bottom = -body_y - lw / 2

    # Determine vertical endpoints from pad extents across all pads
    pads = list(pattern.pads.values())
    if pads:
        pad_clearance = 0.2
        pad_top = max(abs(p.y) + p.height / 2 for p in pads) + pad_clearance
        pad_bottom = -pad_top

        # Use molded preamble for text positioning (same rotation/placement)
        molded_preamble(pattern, housing)

        # Top U (∩)
        top_vertical_end = pad_top + lw / 2
        pattern.line(body_left, body_top, body_left, top_vertical_end)
        pattern.line(body_left, body_top, body_right, body_top)
        pattern.line(body_right, body_top, body_right, top_vertical_end)

        # Bottom U (∪)
        bottom_vertical_end = pad_bottom - lw / 2
        pattern.line(body_left, body_bottom, body_left, bottom_vertical_end)
        pattern.line(body_left, body_bottom, body_right, body_bottom)
        pattern.line(body_right, body_bottom, body_right, bottom_vertical_end)

        # Polarity dot near the leftmost pad if polarized
        if housing.get('polarized'):
            leftmost = min(pads, key=lambda p: p.x)
            # Place dot center 0.6mm from pad edge toward left (closer than previous 0.8mm)
            dot_x = leftmost.x - leftmost.width / 2 - 0.6
            dot_y = 0
            pattern.layer('topSilkscreen').lineWidth(0.1).fill(True).circle(dot_x, dot_y, 0.2).fill(False)
    else:
        # Fallback
        molded_preamble(pattern, housing)


def two_pin(pattern, housing):
    # Use SODFL-specific silkscreen for SODFL components
    if housing.get('sodfl'):
        sodfl(pattern, housing)
        return  # Early return since sodfl() handles everything
    # Use molded-specific silkscreen for molded components
    elif housing.get('molded'):
        molded(pattern, housing)
        return  # Early return since molded() handles everything
    
    s = pattern.settings
    lw = s['lineWidth']['silkscreen']
    first_pad = list(pattern.pads.values())[0]
    gap = lw / 2 + s['clearance']['padToSilk']
    
    if 'bodyWidth' in housing and 'bodyLength' in housing:
        w = housing['bodyWidth']['nom']
        l = housing['bodyLength']['nom']
        
        if housing.get('chip'):
            # For chip: KiCad-style horizontal lines between the pads (in the center gap)
            # Calculate silk line positions avoiding pads
            silk_pad_clearance = s['clearance']['silkToPad']
            silk_line_width = s['lineWidth']['silkscreen']
            silk_pad_offset = silk_pad_clearance + silk_line_width / 2
            
            # Calculate line position - between the pads horizontally
            # Pad 1 is at negative x, pad 2 is at positive x
            # We want lines in the center gap between them
            
            # Get the gap between pads
            pad1_right_edge = first_pad.x + first_pad.width / 2  # right edge of left pad
            pad2_left_edge = -first_pad.x - first_pad.width / 2  # left edge of right pad (symmetric)
            
            # Line starts after pad1 + clearance, ends before pad2 - clearance
            line_start_x = pad1_right_edge + silk_pad_offset
            line_end_x = pad2_left_edge - silk_pad_offset
            
            # Calculate Y positions - based on body outline plus small offset
            # In KiCad, lines are positioned just outside the body outline
            silk_fab_offset = 0.1  # KiCad's silk_fab_offset (typically 0.1mm)
            silk_y = w / 2 + silk_fab_offset
            
            # Only draw lines if they would be long enough and there's space between pads
            min_line_length = 0.2  # minimum 0.2mm line length
            line_length = line_end_x - line_start_x
            if line_length > min_line_length:
                # Use chip-specific preamble for refDes positioning
                chip_preamble(pattern, housing)
                # Two horizontal lines in the center gap between pads
                pattern.line(line_start_x, silk_y, line_end_x, silk_y)
                pattern.line(line_start_x, -silk_y, line_end_x, -silk_y)
                
                if housing.get('polarized'):
                    # Polarity mark near pad 1 (left side)
                    mark_x = first_pad.x - first_pad.width / 2 - silk_pad_offset - 0.1
                    pattern.circle(mark_x, 0, 0.05)
            else:
                # Lines too short, just add the preamble for refDes positioning
                chip_preamble(pattern, housing)
        else:
            # Standard two-pin orientation
            x1 = first_pad.width / 2 + gap
            x2 = w / 2 + lw / 2
            x = max(x1, x2)
            y = l / 2 + lw / 2
            preamble(pattern, housing)
            if housing.get('cae') and not housing.get('nosilk'):
                # CAE silkscreen: split into top and bottom patterns, avoiding pads
                s = pattern.settings
                lw = s['lineWidth']['silkscreen']
                silk_pad_clearance = s['clearance']['silkToPad']
                
                bw = housing['bodyWidth']['nom']
                bl = housing['bodyLength']['nom']
                
                # Use custom chamfer if provided, otherwise use default
                custom_chamfer = housing.get('chamfer')
                if custom_chamfer and str(custom_chamfer).strip():
                    try:
                        d = float(custom_chamfer)
                    except (ValueError, TypeError):
                        d = min(bw / 4, bl / 4)  # Fallback to default
                else:
                    d = min(bw / 4, bl / 4)  # Default chamfer size
                
                # Add 0.06mm to perimeter (0.06mm on each side)
                x_silk = bw / 2 + 0.06
                y_silk = bl / 2 + 0.06
                d_silk = d + 0.06  # Also add to chamfer
                
                # Get pad positions to avoid them
                pads = list(pattern.pads.values())
                pad1 = pads[0]  # Left pad
                pad2 = pads[1]  # Right pad
                
                # Calculate pad boundaries with clearance (including silkscreen line width)
                pad_clearance = silk_pad_clearance + lw / 2  # Total clearance including line width
                pad1_bottom = pad1.y + pad1.height / 2 + pad_clearance
                pad1_top = pad1.y - pad1.height / 2 - pad_clearance
                pad2_bottom = pad2.y + pad2.height / 2 + pad_clearance
                pad2_top = pad2.y - pad2.height / 2 - pad_clearance
                
                # Top pattern: chamfered top with vertical lines avoiding pads
                # Top horizontal line with chamfer
                pattern.moveTo(-x_silk, pad1_top).lineTo(-x_silk, -y_silk + d_silk).lineTo(-x_silk + d_silk, -y_silk).lineTo(x_silk, -y_silk).lineTo(x_silk, pad2_top)
                
                
                # Bottom pattern: chamfered bottom with vertical lines avoiding pads
                # Bottom horizontal line with chamfer
                pattern.moveTo(-x_silk, pad1_bottom).lineTo(-x_silk, y_silk - d_silk).lineTo(-x_silk + d_silk, y_silk).lineTo(x_silk, y_silk).lineTo(x_silk, pad2_bottom)
                
                # Add pin 1 indicator dot to the left of pin1
                if housing.get('polarized'):
                    dot_x = pad1.x - pad1.width / 2 - 0.4 - 0.1  # 0.2mm clearance + 0.1mm spacing
                    dot_y = pad1.y
                    pattern.layer('topSilkscreen').lineWidth(0.1).fill(True).circle(dot_x, dot_y, 0.2).fill(False)
            elif not housing.get('nosilk'):
                pattern.line(-x, -y, -x, y).line(x, -y, x, y)
                if x1 < x2:  # Molded
                    pattern.line(-x1, -y, -x2, -y).line(-x1, y, -x2, y).line(x1, -y, x2, -y).line(x1, y, x2, y)
    elif 'bodyDiameter' in housing:
        r = housing['bodyDiameter']['nom'] / 2 + lw / 2
        preamble(pattern, housing)
        if not housing.get('nosilk'):
            pattern.circle(0, 0, r)
        if housing.get('polarized'):
            y = first_pad.y + first_pad.height / 2 + gap
            pattern.rectangle(-first_pad.width / 2 - gap, -r, first_pad.width / 2 + gap, y)
            pattern.circle(0, -r - 1.5 * lw, 0)

