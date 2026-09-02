from __future__ import annotations
import json, re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / 'data'
SETTINGS_FILE = DATA_DIR / 'print_settings.json'

DEFAULTS = {
    'font': 'Liberation Serif Bold',
    'text_scale': 1.0,
    'logo_scales': {'flight': 1.25, 'bus': 1.0, 'hotel': 1.0, 'package': 1.0},
    'watermark_opacity': 0.04,
    'watermark_scale': 1.5,
    'default_terms': 'tc_non_google',
    'tour_last_page': 'tc_non_google',
    'footer_defaults': {'flight': 'footer2', 'bus': 'footer2', 'hotel': 'footer2', 'package': 'footer2'},
    'buttons': {
        'make_changes': True,
        'add_cost': True,
        'print_without_fare': True,
        'print_original_fare': True,
        'footer_bar': True,
        'footer_design': True,
        'footer2': True,
        'print_clean': True,
        'page_size_controls': True,
        'watermark': True,
        'main_tour': True,
        'main_air': True,
        'main_bus': True,
        'main_hotel': True,
        'main_ai': True,
        'main_edit': False,
        'main_files': False,
        'main_settings': True,
    },
}
FONT_OPTIONS = [
    'Liberation Serif Bold',
    'Liberation Serif',
    'Liberation Sans Bold',
    'Liberation Sans',
    'Calibri',
    'Arial',
]


def _merge(a, b):
    out = dict(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_settings():
    DATA_DIR.mkdir(exist_ok=True)
    if not SETTINGS_FILE.exists():
        save_settings(DEFAULTS)
        return json.loads(json.dumps(DEFAULTS))
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding='utf-8'))
        if isinstance(data, dict) and not data.get('_air_logo_125_migrated'):
            scales = data.get('logo_scales') if isinstance(data.get('logo_scales'), dict) else {}
            try:
                if abs(float(scales.get('flight', 1.0)) - 1.0) < 0.001:
                    scales['flight'] = 1.25
            except Exception:
                scales['flight'] = 1.25
            data['logo_scales'] = scales
            data['_air_logo_125_migrated'] = True
            save_settings(data)
        merged = _merge(DEFAULTS, data if isinstance(data, dict) else {})
        if merged.get('font') not in FONT_OPTIONS:
            merged['font'] = DEFAULTS['font']
        merged['text_scale'] = max(0.75, min(1.35, float(merged.get('text_scale', 1.0))))
        ls = merged.get('logo_scales') if isinstance(merged.get('logo_scales'), dict) else {}
        merged['logo_scales'] = {k: max(0.70, min(1.50, float(ls.get(k, 1.25 if k=='flight' else 1.0)))) for k in ('flight','bus','hotel','package')}
        merged['watermark_opacity'] = max(0.01, min(0.20, float(merged.get('watermark_opacity', 0.04))))
        merged['watermark_scale'] = max(0.50, min(2.00, float(merged.get('watermark_scale', 1.5))))
        # V94 uses one Tour T&C page only (the supplied T&C 2 document).
        merged['default_terms'] = 'tc_non_google'
        merged['tour_last_page'] = merged.get('tour_last_page', 'tc_non_google') if merged.get('tour_last_page') in ('without_footer','tc_non_google') else 'tc_non_google'
        defaults = {'flight':'footer2','bus':'footer2','hotel':'footer2','package':'footer2'}
        current = merged.get('footer_defaults') if isinstance(merged.get('footer_defaults'), dict) else {}
        merged['footer_defaults'] = {k: current.get(k, v) if current.get(k, v) in ('design','footer2','bar') else v for k,v in defaults.items()}
        merged['buttons']['watermark'] = bool(merged['buttons'].get('watermark', True))
        return merged
    except Exception:
        return json.loads(json.dumps(DEFAULTS))


def save_settings(settings):
    DATA_DIR.mkdir(exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2, ensure_ascii=False), encoding='utf-8')


def set_font(name):
    s = load_settings()
    if name not in FONT_OPTIONS:
        raise ValueError('Unsupported font option.')
    s['font'] = name
    save_settings(s)
    return s


def adjust_text_scale(delta):
    s = load_settings()
    current = float(s.get('text_scale', 1.0))
    current = round(max(0.75, min(1.35, current + float(delta))), 2)
    s['text_scale'] = current
    save_settings(s)
    return s




def adjust_logo_scale(kind, delta):
    kind = str(kind)
    if kind not in ('flight','bus','hotel','package'):
        raise ValueError('Unsupported document type.')
    s = load_settings()
    s.setdefault('logo_scales', {})
    current = float(s['logo_scales'].get(kind, 1.25 if kind=='flight' else 1.0))
    s['logo_scales'][kind] = round(max(0.70, min(1.50, current + float(delta))), 2)
    save_settings(s)
    return s

def get_logo_scale(kind):
    s = load_settings()
    return float(s.get('logo_scales', {}).get(str(kind), 1.25 if str(kind)=='flight' else 1.0))

def toggle_button(key):
    s = load_settings()
    if key not in s['buttons']:
        raise ValueError('Unknown button setting.')
    s['buttons'][key] = not bool(s['buttons'][key])
    save_settings(s)
    return s


def reset_settings():
    s = json.loads(json.dumps(DEFAULTS))
    save_settings(s)
    return s


def button_enabled(key):
    return bool(load_settings()['buttons'].get(key, True))


def set_default_terms(choice):
    # Kept for backward compatibility; V94 always uses the single supplied T&C page.
    s = load_settings()
    s['default_terms'] = 'tc2'
    save_settings(s)
    return s


def set_default_footer(kind, mode):
    kind = str(kind)
    if kind not in ('flight','bus','hotel','package'):
        raise ValueError('Unsupported document type.')
    if mode not in ('design','footer2','bar'):
        raise ValueError('Unsupported footer mode.')
    s = load_settings()
    s.setdefault('footer_defaults', {})
    s['footer_defaults'][kind] = mode
    save_settings(s)
    return s

def get_default_footer(kind):
    s = load_settings()
    return s.get('footer_defaults', {}).get(str(kind), 'footer2')

def set_tour_last_page(choice):
    if choice not in ('without_footer','tc_non_google'):
        raise ValueError('Unsupported Tour last-page option.')
    s=load_settings(); s['tour_last_page']=choice; save_settings(s); return s

def get_tour_last_page():
    s=load_settings(); return s.get('tour_last_page','tc_non_google')

def _font_paths():
    return {
        'Liberation Serif Bold': (BASE_DIR / 'assets' / 'LiberationSerif-Bold.ttf', 'MTBLiberationSerifBold', 700),
        'Liberation Serif': (BASE_DIR / 'assets' / 'LiberationSerif-Regular.ttf', 'MTBLiberationSerif', 400),
        'Liberation Sans Bold': (BASE_DIR / 'assets' / 'LiberationSans-Bold.ttf', 'MTBLiberationSansBold', 700),
        'Liberation Sans': (BASE_DIR / 'assets' / 'LiberationSans-Regular.ttf', 'MTBLiberationSans', 400),
        'Calibri': (Path(r'C:\Windows\Fonts\calibri.ttf'), 'MTBCalibri', 400),
        'Arial': (Path(r'C:\Windows\Fonts\arial.ttf'), 'MTBArial', 400),
    }


def css_injection():
    """Return CSS that applies the current global print font and text scale."""
    s = load_settings()
    name = s['font']
    path, family, weight = _font_paths()[name]
    if path.exists():
        src = path.resolve().as_uri()
        face = f"@font-face{{font-family:'{family}';src:url('{src}');font-weight:{weight};font-style:normal;}}"
    else:
        face = ''
    fallback = "'Times New Roman',serif" if 'Serif' in name else "Arial,sans-serif"
    family_css = f"'{family}',{fallback}" if path.exists() else ("Calibri,Arial,sans-serif" if name == 'Calibri' else "Arial,sans-serif")
    scale = float(s.get('text_scale', 1.0))
    # Replace explicit font-family declarations and multiply all px/pt font sizes.
    return face + f"\n*{{font-family:{family_css} !important;}}\n:root{{--mtb-text-scale:{scale};}}\n"


def apply_css_settings(html: str, kind=None, text_scale_override=None, logo_scale_override=None) -> str:
    """Apply global settings to an already-rendered HTML template."""
    css = css_injection()
    out = str(html)
    # Insert our font-face/override immediately after <style> where possible.
    if '<style>' in out:
        out = out.replace('<style>', '<style>' + css, 1)
    else:
        out = '<style>' + css + '</style>' + out
    settings = load_settings()
    scale = float(settings['text_scale'] if text_scale_override is None else text_scale_override)
    logo_scale = float(logo_scale_override if logo_scale_override is not None else settings.get('logo_scales', {}).get(str(kind), 1.25 if str(kind)=='flight' else 1.0))
    logo_css = f"\n.logo, .logo img, .brand-logo, .banner .logo img {{ transform:scale({logo_scale}); transform-origin:left center; }}\n"
    out = out.replace('</style>', logo_css + '</style>', 1)
    # Scale explicit font sizes so the owner can change print readability globally.
    def repl(m):
        num = m.group(1)
        unit = m.group(2)
        try:
            val = float(num) * float(scale)
            rendered = ('%.3f' % val).rstrip('0').rstrip('.')
            return f'font-size:{rendered}{unit}'
        except Exception:
            return m.group(0)
    out = re.sub(r'font-size\s*:\s*([0-9]+(?:\.[0-9]+)?)(pt|px)', repl, out, flags=re.I)
    return out
