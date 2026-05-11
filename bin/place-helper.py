#!/usr/bin/env python3
"""Spatial helper for placing instances into NWN <area>.git.json files.

Surveys all instances in an area, evaluates clearance at a target XY, and
suggests known-reachable coordinates that are clear of any placeable.

Usage:
    bin/place-helper.py <area_resref>                 # area overview + map
    bin/place-helper.py <area_resref> <x> <y>         # clearance report for (x,y)
    bin/place-helper.py <area_resref> --suggest       # propose N clear spots

Examples:
    bin/place-helper.py thewelloferu
    bin/place-helper.py thewelloferu 20 12
    bin/place-helper.py rivendellupperha --suggest
"""
import json, sys, os, math, argparse, glob

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UNPACKED = os.path.join(REPO, 'unpacked')

# Approximate physical footprint of common categories (radius in metres).
# Tuned conservatively — bigger = pickier about clearance.
RADIUS = {
    'creature':   1.0,    # personal space; creatures walk around each other
    'placeable':  2.5,    # most placeables are 2-3m wide
    'door':       2.0,
    'trigger':    0.0,    # triggers are zones not obstacles
    'encounter':  0.0,
    'waypoint':   0.0,
    'sound':      0.0,
    'store':      2.0,
}
LIST_TO_CATEGORY = {
    'Creature List':   'creature',
    'Placeable List':  'placeable',
    'Door List':       'door',
    'TriggerList':     'trigger',
    'Encounter List':  'encounter',
    'WaypointList':    'waypoint',
    'SoundList':       'sound',
    'StoreList':       'store',
}

def load_area(resref):
    are_path = os.path.join(UNPACKED, f'{resref}.are.json')
    git_path = os.path.join(UNPACKED, f'{resref}.git.json')
    if not os.path.exists(are_path):
        sys.exit(f'No area file: {are_path}')
    with open(are_path) as f: are = json.load(f)
    with open(git_path) as f: git = json.load(f)
    width  = are['Width']['value']  * 10  # 10m per tile
    height = are['Height']['value'] * 10
    return are, git, width, height

def position_of(inst):
    """Read XY from either creature-style or placeable-style position fields."""
    if 'X' in inst:
        return inst['X']['value'], inst['Y']['value']
    if 'XPosition' in inst:
        return inst['XPosition']['value'], inst['YPosition']['value']
    return None

def all_instances(git):
    """Yield (category, tag, tres, x, y) for every placed thing in the area."""
    for lk, cat in LIST_TO_CATEGORY.items():
        for inst in git.get(lk, {}).get('value', []):
            pos = position_of(inst)
            if pos is None: continue
            x, y = pos
            tag = inst.get('Tag', {}).get('value', '')
            tres = inst.get('TemplateResRef', {}).get('value', '')
            yield (cat, tag, tres, x, y)

def clearance(target_x, target_y, instances, ignore_tag=None):
    """Return (worst_obstacle_or_None, distance) — None means clear."""
    worst = None
    worst_dist = math.inf
    for (cat, tag, tres, x, y) in instances:
        if tag == ignore_tag: continue
        r = RADIUS.get(cat, 0)
        if r == 0: continue
        d = math.hypot(x - target_x, y - target_y)
        slack = d - r
        if slack < worst_dist:
            worst_dist = slack
            worst = (cat, tag, tres, x, y, d, r)
    return worst, worst_dist

def report_target(resref, tx, ty):
    are, git, W, H = load_area(resref)
    instances = list(all_instances(git))
    print(f'Area: {resref}  ({W}x{H} m)  target: ({tx}, {ty})')
    if not (0 <= tx <= W and 0 <= ty <= H):
        print('  ⚠ OUT OF BOUNDS')
    # Nearest 10 instances
    ranked = sorted(instances, key=lambda i: math.hypot(i[3]-tx, i[4]-ty))[:10]
    print('\nNearest 10 instances:')
    for cat, tag, tres, x, y in ranked:
        d = math.hypot(x-tx, y-ty)
        r = RADIUS.get(cat, 0)
        flag = '⚠ COLLIDE' if d < r else ('• tight' if d < r + 1.5 else '  ok')
        print(f'  {flag}  {cat:9s} d={d:5.1f}m  r={r:.1f}  ({x:5.1f},{y:5.1f})  {tag or tres}')
    worst, slack = clearance(tx, ty, instances)
    if worst:
        cat, tag, tres, x, y, d, r = worst
        if slack < 0:
            print(f'\nVERDICT: COLLIDES with {cat} "{tag or tres}" at ({x:.1f},{y:.1f}) — its r={r:.1f} but distance is only {d:.1f}m')
        else:
            print(f'\nVERDICT: clear (closest obstacle: {cat} "{tag or tres}" at {d:.1f}m, slack {slack:.1f}m)')
    # Reachability proxy: nearest creature (creatures live on walkable tiles)
    creatures = [i for i in instances if i[0]=='creature']
    if creatures:
        nearest_c = min(creatures, key=lambda i: math.hypot(i[3]-tx, i[4]-ty))
        d = math.hypot(nearest_c[3]-tx, nearest_c[4]-ty)
        print(f'Nearest creature (reachability anchor): {nearest_c[1] or nearest_c[2]} at ({nearest_c[3]:.1f},{nearest_c[4]:.1f}) — {d:.1f}m away')
        if d > 15:
            print(f'  ⚠ {d:.0f}m is far — target may be on unreachable terrain (no creature within 15m)')

def suggest_spots(resref, n=8, near_creature_max=8.0, placeable_min=4.0):
    """Find candidate (x,y) near existing creatures, far from placeables."""
    are, git, W, H = load_area(resref)
    instances = list(all_instances(git))
    creatures = [i for i in instances if i[0]=='creature']
    if not creatures:
        print('No creatures in this area to anchor reachability — falling back to grid sweep.')
        creatures = [('creature','','',W/2,H/2)]
    suggestions = []
    # Sample rings around each creature anchor
    for anc in creatures:
        ax, ay = anc[3], anc[4]
        for r in (2.5, 4.0, 6.0):
            for ang_deg in range(0, 360, 30):
                ang = math.radians(ang_deg)
                cx = ax + r*math.cos(ang)
                cy = ay + r*math.sin(ang)
                if not (1 < cx < W-1 and 1 < cy < H-1): continue
                worst, slack = clearance(cx, cy, instances)
                if slack < placeable_min - 2.5: continue  # too close to a placeable
                # Distance to nearest creature must be small (proxy for reachability)
                nc = min(creatures, key=lambda i: math.hypot(i[3]-cx, i[4]-cy))
                ncd = math.hypot(nc[3]-cx, nc[4]-cy)
                if ncd > near_creature_max: continue
                suggestions.append((slack, cx, cy, anc[1] or anc[2], ncd))
    # Dedupe nearby suggestions
    suggestions.sort(reverse=True)
    chosen = []
    for s in suggestions:
        slack, cx, cy, anc, ncd = s
        if any(math.hypot(cx-c[1], cy-c[2]) < 3 for c in chosen): continue
        chosen.append((cx, cy, slack, anc, ncd))
        if len(chosen) >= n: break
    print(f'Suggested clear spots in {resref}:')
    print(f'  (anchored ≤{near_creature_max}m from an existing creature, ≥{placeable_min}m clearance from any placeable)')
    for cx, cy, slack, anc, ncd in chosen:
        print(f'  ({cx:5.1f},{cy:5.1f})  clearance={slack:.1f}m   anchor={anc} ({ncd:.1f}m away)')

def overview(resref):
    are, git, W, H = load_area(resref)
    instances = list(all_instances(git))
    print(f'Area: {resref}  ({W}x{H} m)')
    by_cat = {}
    for inst in instances:
        by_cat.setdefault(inst[0], []).append(inst)
    for cat, items in sorted(by_cat.items()):
        print(f'  {cat:9s} {len(items):4d}')

    # ASCII map (1 char = ~3m)
    cols = max(20, int(W/3))
    rows = max(10, int(H/3))
    grid = [[' ']*cols for _ in range(rows)]
    sym = {'creature':'C', 'placeable':'p', 'door':'D', 'trigger':'t', 'encounter':'e', 'waypoint':'w', 'sound':'s', 'store':'$'}
    for cat, tag, tres, x, y in instances:
        col = int(x / W * (cols-1))
        row = (rows-1) - int(y / H * (rows-1))
        if 0 <= row < rows and 0 <= col < cols:
            cur = grid[row][col]
            grid[row][col] = sym.get(cat, '?') if cur == ' ' else '*'
    print(f'\n  ASCII map (origin SW; ~{W/cols:.1f}m per column, ~{H/rows:.1f}m per row):')
    print('  +' + '-'*cols + '+')
    for r in grid:
        print('  |' + ''.join(r) + '|')
    print('  +' + '-'*cols + '+')
    print('  legend: C=creature p=placeable D=door t=trigger e=encounter w=waypoint *=overlap')

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('area', help='area resref (filename without .are.json)')
    ap.add_argument('x', nargs='?', type=float)
    ap.add_argument('y', nargs='?', type=float)
    ap.add_argument('--suggest', action='store_true', help='propose clear spots')
    ap.add_argument('--n', type=int, default=8, help='number of suggestions')
    args = ap.parse_args()
    if args.suggest:
        suggest_spots(args.area, n=args.n)
    elif args.x is not None and args.y is not None:
        report_target(args.area, args.x, args.y)
    else:
        overview(args.area)

if __name__ == '__main__':
    main()
