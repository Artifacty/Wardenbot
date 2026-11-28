"""PNG renderer for public map objects. No Discord or network dependencies."""
from __future__ import annotations

import json
import math
import re
import textwrap
from datetime import datetime
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageEnhance, ImageFont

ASSETS = Path(__file__).parent / 'assets' / 'war_map'
REGIONS = json.loads((ASSETS / 'regions.json').read_text(encoding='utf-8'))
REGION_INDEX = {name.lower().removesuffix('hex'): value for name, value in REGIONS.items()}
COLORS = {'WARDENS': '#68acff', 'COLONIALS': '#a5bd70', 'NONE': '#eeeeed'}
BASES = {27, 45, 46, 47, 56, 57, 58}
SECTOR_BASES = {45, 46, 47, 56, 57, 58}
SECTOR_COLORS = {'WARDENS': (78, 153, 217, 116),
                 'COLONIALS': (128, 196, 92, 124),
                 'NONE': (182, 188, 190, 65)}
NAVY = '#111d2b'
HEX_WIDTH = 4096 / 10  # 13 columns with 3/4-width pitch, including the naval regions.
HEX_HEIGHT = HEX_WIDTH * math.sqrt(3) / 2
TOP = (4096 - 7 * HEX_HEIGHT) / 2


def region_info(name):
    return REGION_INDEX.get(name.lower().removesuffix('hex'))


def region_name(name):
    info = region_info(name)
    return info['name'] if info else re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', name.removesuffix('Hex'))


@lru_cache(maxsize=16)
def font(size):
    return ImageFont.truetype(str(ASSETS / 'DejaVuSans.ttf'), size)


@lru_cache(maxsize=1)
def background():
    with Image.open(ASSETS / 'background.webp') as source:
        image = source.convert('RGB')
    image = ImageEnhance.Color(image).enhance(0.45)
    return ImageEnhance.Brightness(image).enhance(0.65)


def region_box(info):
    x = info['col'] * HEX_WIDTH * .75
    y = (info['row'] + (info['col'] % 2) * .5) * HEX_HEIGHT
    return x, y, x + HEX_WIDTH, y + HEX_HEIGHT


def point(info, x, y):
    left, top, _, _ = region_box(info)
    return left + x * HEX_WIDTH, top + y * HEX_HEIGHT


def hex_vertices(width, height):
    """Local coordinates of the playable hex, with pointed left/right ends."""
    return [(0, height/2), (width/4, 0), (3*width/4, 0),
            (width, height/2), (3*width/4, height), (width/4, height)]


def clip_nearest(polygon, site, other):
    """Clip a polygon to points at least as close to site as to other."""
    sx, sy = site
    ox, oy = other
    dx, dy = ox-sx, oy-sy
    threshold = (ox*ox + oy*oy - sx*sx - sy*sy) / 2
    def distance(vertex):
        return vertex[0]*dx + vertex[1]*dy - threshold
    result = []
    if not polygon:
        return result
    previous = polygon[-1]
    previous_distance = distance(previous)
    for current in polygon:
        current_distance = distance(current)
        if (previous_distance <= 0) != (current_distance <= 0):
            fraction = previous_distance / (previous_distance-current_distance)
            result.append((previous[0] + fraction*(current[0]-previous[0]),
                           previous[1] + fraction*(current[1]-previous[1])))
        if current_distance <= 0:
            result.append(current)
        previous, previous_distance = current, current_distance
    return result


def sector_polygons(map_items, width, height):
    """Approximate territories from public town/relic bases; API has no zone borders."""
    sites = []
    for item in map_items:
        if item.get('iconType') not in SECTOR_BASES:
            continue
        position = item['x']*width, item['y']*height
        if any(math.dist(position, prior[0]) < .01 for prior in sites):
            continue
        sites.append((position, item.get('teamId', 'NONE')))
    if not sites:
        return []
    bounds = hex_vertices(width, height)
    result = []
    for index, (site, team) in enumerate(sites):
        polygon = bounds[:]
        for other_index, (other, _) in enumerate(sites):
            if index != other_index:
                polygon = clip_nearest(polygon, site, other)
                if not polygon:
                    break
        if len(polygon) >= 3:
            result.append((polygon, team))
    return result


def _text(draw, xy, text, size, *, anchor=None, fill='white'):
    draw.text(xy, text, font=font(size), fill=fill, anchor=anchor, stroke_width=2, stroke_fill=NAVY)


def _marker(draw, x, y, item, radius):
    color = COLORS.get(item.get('teamId'), COLORS['NONE'])
    icon = item.get('iconType')
    flags = item.get('flags', 0)
    if flags & 16:
        color = '#ba8d8d'
        draw.line((x-radius, y-radius, x+radius, y+radius), fill=color, width=3)
        draw.line((x-radius, y+radius, x+radius, y-radius), fill=color, width=3)
    elif flags & 1:
        r = radius + 2
        draw.polygon(((x, y-r), (x+r, y), (x, y+r), (x-r, y)), fill=color, outline=NAVY, width=2)
    elif icon in BASES:
        draw.rectangle((x-radius, y-radius, x+radius, y+radius), fill=color, outline=NAVY, width=2)
    else:
        draw.ellipse((x-radius, y-radius, x+radius, y+radius), fill=color, outline=NAVY, width=2)


def unknown_regions(snapshot):
    return [name for name in snapshot['regions'] if region_info(name) is None]


def render(snapshot, region=None):
    """Return a PNG for the full map or a single region using the same coordinates."""
    detailed = region is not None
    width = 1280 if detailed else 1800
    map_height = round(width * math.sqrt(3) / 2) if detailed else round(width * 7 * HEX_HEIGHT / 4096)
    header, footer = 82, 108
    image = Image.new('RGB', (width, map_height + header + footer), NAVY)
    info = region_info(region) if detailed else None
    if detailed and info:
        left, top, right, bottom = region_box(info)
        base = background().crop((round(left), round(top+TOP), round(right), round(bottom+TOP)))
    elif detailed:
        base = Image.new('RGB', (width, map_height), '#253748')
    else:
        base = background().crop((0, round(TOP), 4096, round(TOP+7*HEX_HEIGHT)))
    image.paste(base.resize((width, map_height), Image.Resampling.LANCZOS), (0, header))
    active = snapshot['regions']
    # Paint full sector polygons under labels and icons. A separate transparent
    # layer keeps the terrain visible and prevents alpha accumulating at edges.
    tint = Image.new('RGBA', image.size)
    tint_draw = ImageDraw.Draw(tint)
    for name, region_data in active.items():
        meta = region_info(name)
        if detailed and name != region or not detailed and meta is None:
            continue
        if detailed:
            left, top, sector_width, sector_height = 0, header, width, map_height
        else:
            left, top, _, _ = region_box(meta)
            scale = width / 4096
            left, top = left*scale, top*scale+header
            sector_width, sector_height = HEX_WIDTH*scale, HEX_HEIGHT*scale
        for polygon, team in sector_polygons(region_data['dynamic']['mapItems'],
                                             sector_width, sector_height):
            points = [(left+x, top+y) for x, y in polygon]
            tint_draw.polygon(points, fill=SECTOR_COLORS.get(team, SECTOR_COLORS['NONE']))
    image = Image.alpha_composite(image.convert('RGBA'), tint).convert('RGB')
    draw = ImageDraw.Draw(image)
    title = region_name(region) if detailed else 'КАРТА ВОЙНЫ'
    _text(draw, (24, 12), title, 29)
    checked = datetime.fromtimestamp(snapshot['checked_at'], ZoneInfo('Europe/Moscow'))
    _text(draw, (24, 50), f"{snapshot['shard'].title()} · Война №{snapshot['war']['warNumber']} · Проверено {checked:%d.%m %H:%M} МСК", 17)
    if not detailed:
        scale = width / 4096
        for name, meta in REGIONS.items():
            left, top, right, bottom = region_box(meta)
            cx, cy = (left+right)/2, (top+bottom)/2
            pts = [(left,cy),(left+HEX_WIDTH/4,top),(right-HEX_WIDTH/4,top),
                   (right,cy),(right-HEX_WIDTH/4,bottom),(left+HEX_WIDTH/4,bottom)]
            draw.line([(x*scale, y*scale+header) for x,y in pts+[pts[0]]], fill='#263442', width=2)
    for name, data in active.items():
        meta = region_info(name)
        if detailed and name != region or not detailed and meta is None:
            continue
        for item in data['dynamic']['mapItems']:
            if not detailed and item.get('iconType') not in BASES and not item.get('flags', 0) & 1:
                continue
            x, y = (item['x']*width, item['y']*map_height) if detailed else (
                v * width/4096 for v in point(meta, item['x'], item['y']))
            _marker(draw, x, y+header, item, 7 if detailed else 4)
        if not detailed:
            x,y = point(meta, .5, .24)
            label = '\n'.join(textwrap.wrap(region_name(name), 18))
            draw.multiline_text((x*width/4096, y*width/4096+header), label, font=font(12),
                                anchor='mm', align='center', fill='white', stroke_width=2, stroke_fill=NAVY)
        else:
            placed = []
            for item in data['static'].get('mapTextItems', []):
                if item.get('mapMarkerType') != 'Major':
                    continue
                text = str(item.get('text', ''))[:70]
                x = max(12, min(width-12, item['x']*width))
                y = max(header+12, min(header+map_height-35, item['y']*map_height+header))
                box = draw.textbbox((x,y), text, font=font(16), anchor='mt')
                dx = max(0, 8-box[0]) - max(0, box[2]-(width-8))
                box = (box[0]+dx,box[1],box[2]+dx,box[3])
                if any(not (box[2]<b[0] or box[0]>b[2] or box[3]<b[1] or box[1]>b[3]) for b in placed):
                    continue
                placed.append(box)
                _text(draw, (x+dx,y), text, 16, anchor='mt')
    y = header + map_height + 18
    for index,(team,label) in enumerate((('WARDENS','Вардены'),('COLONIALS','Колонисты'),('NONE','Нейтральные'))):
        x=24+index*220
        draw.rectangle((x,y,x+16,y+16), fill=COLORS[team])
        _text(draw,(x+25,y-2),label,18)
    _text(draw,(24,y+32),'Заливка: ближайшая база (границы приблизительные) · метки: контроль объектов',15)
    note = 'Foxhole War API · Карта: Siege Camp / Kastow / Sentsu / NoUDerp'
    if detailed and not info:
        note = 'Схема координат: фон нового региона ещё не добавлен · Foxhole War API'
    _text(draw,(24,y+59),note,13,fill='#b7c3d0')
    out=BytesIO()
    image.save(out, format='PNG', optimize=True)
    return out.getvalue()
