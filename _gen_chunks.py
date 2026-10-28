"""Split the full timeline (_timeline.json) into N self-contained chunk
compositions (chunk0.html .. chunk{N-1}.html) for cloud rendering.

Generic across long-form (landscape, N_CHUNKS>1, parallel render) and short-form
(portrait, N_CHUNKS=1, single render) — canvas + chunk count come from canvas.json,
overridable via `python _gen_chunks.py <n_chunks> [--canvas WxH]`.

Each chunk holds only the clips/captions/flashes inside its time window,
re-based to start at 0, with NO audio (the full mixed audio is muxed over the
stitched video afterward). Chunk seams are hard cuts — imperceptible at a few
per multi-minute montage.

Output: chunk0.html..chunk{N-1}.html at project root, plus _chunks.json manifest.
"""
import json, sys

canvas = json.load(open('canvas.json')) if __import__('os').path.exists('canvas.json') else {'width': 1920, 'height': 1080, 'chunks': 4}
N_CHUNKS = canvas.get('chunks', 4)
W, H = canvas['width'], canvas['height']
for a in sys.argv[1:]:
    if a.startswith('--canvas'):
        wh = a.split('=', 1)[1] if '=' in a else sys.argv[sys.argv.index(a) + 1]
        W, H = (int(x) for x in wh.lower().split('x'))
    elif a.isdigit():
        N_CHUNKS = int(a)
PORTRAIT = H > W

T = json.load(open('_timeline.json'))
COMP = T['comp']; CTA_AT = T['cta_at']; CTA = T['cta']
CLIPS = T['clips']; CAPS = T['caps']; FLASHES = T['flashes']
MS = 0.6
N = len(CLIPS)

# group boundaries: clip indices where global start crosses comp*g/N
bounds = [0]
for g in range(1, N_CHUNKS):
    target = COMP * g / N_CHUNKS
    i = next((k for k in range(N) if CLIPS[k]['start'] >= target), N)
    if i <= bounds[-1]:
        i = bounds[-1] + 1
    bounds.append(min(i, N))
bounds.append(N)

# Two brand-consistent presets tuned per orientation (colors/fonts identical,
# type scale + safe-area padding differ — mobile needs bigger captions & a
# taller bottom safe zone for the platform UI).
if PORTRAIT:
    CAP_FONT, CAP_MIN, CAP_MAXW, CAP_PAD, CAP_BOTTOM = 92, 54, W - 200, 70, 540
    CTA_Q_FONT, CHIP_FONT, FOLLOW_FONT, CTA_GAP, CTA_PAD = 76, 84, 46, 46, 80
else:
    CAP_FONT, CAP_MIN, CAP_MAXW, CAP_PAD, CAP_BOTTOM = 66, 42, 1480, 210, 96
    CTA_Q_FONT, CHIP_FONT, FOLLOW_FONT, CTA_GAP, CTA_PAD = 84, 72, 44, 54, 120

STYLE = f'''
      html, body {{ margin:0; padding:0; width:{W}px; height:{H}px; overflow:hidden; background:#05060a; font-family:"Montserrat", sans-serif; }}
      #root {{ position:relative; width:{W}px; height:{H}px; overflow:hidden; background:#05060a; }}
      .layer {{ position:absolute; inset:0; width:{W}px; height:{H}px; overflow:hidden; opacity:0; }}
      #L0 {{ opacity:1; }}
      .zoom {{ position:absolute; inset:0; width:100%; height:100%; transform-origin:center center; }}
      .layer video {{ position:absolute; inset:0; width:100%; height:100%; object-fit:cover; }}
      #grade {{ position:absolute; inset:0; z-index:140; pointer-events:none;
        background:linear-gradient(to bottom, rgba(5,6,12,0.5) 0%, rgba(5,6,12,0) 22%, rgba(5,6,12,0) 55%, rgba(5,6,12,0.85) 100%); }}
      #tint {{ position:absolute; inset:0; z-index:141; pointer-events:none; mix-blend-mode:soft-light;
        background:radial-gradient(120% 100% at 50% 28%, rgba(92,45,145,0.5) 0%, rgba(10,22,40,0.0) 62%); }}
      #vignette {{ position:absolute; inset:0; z-index:142; pointer-events:none;
        background:radial-gradient(120% 120% at 50% 45%, rgba(0,0,0,0) 55%, rgba(0,0,0,0.55) 100%); }}
      #cap {{ position:absolute; left:0; right:0; bottom:{CAP_BOTTOM}px; z-index:160; text-align:center; pointer-events:none; }}
      .cg {{ position:absolute; left:0; right:0; bottom:0; opacity:0; visibility:hidden;
        font-family:"Montserrat", sans-serif; font-weight:800; font-size:{CAP_FONT}px; line-height:1.06; color:#ffffff;
        padding:0 {CAP_PAD}px; box-sizing:border-box; text-transform:uppercase; letter-spacing:-0.005em;
        -webkit-text-stroke:6px #000; paint-order:stroke fill; text-shadow:0 5px 18px rgba(0,0,0,0.85); }}
      .cg .hl {{ color:#FFD600; }}
      #progwrap {{ position:absolute; left:0; bottom:0; width:{W}px; height:8px; z-index:159; background:rgba(255,255,255,0.10); }}
      #prog {{ position:absolute; left:0; top:0; height:8px; width:{W}px; background:#00B4D8; transform-origin:left center; transform:scaleX(0); box-shadow:0 0 12px rgba(0,180,216,0.8); }}
      #flash {{ position:absolute; inset:0; z-index:166; background:#ffffff; opacity:0; pointer-events:none; mix-blend-mode:screen; }}
      #cta {{ position:absolute; inset:0; z-index:170; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:{CTA_GAP}px; opacity:0; visibility:hidden; text-align:center; padding:0 {CTA_PAD}px; box-sizing:border-box; }}
      #cta-bg {{ position:absolute; inset:0; z-index:-1; background:radial-gradient(120% 100% at 50% 45%, rgba(26,5,51,0.90) 0%, rgba(10,22,40,0.95) 100%); }}
      #cta-pre {{ font-weight:800; font-size:52px; color:#ffffff; text-transform:uppercase; letter-spacing:0.06em; text-shadow:0 4px 16px rgba(0,0,0,0.85); }}
      #cta-q {{ font-weight:800; font-size:{CTA_Q_FONT}px; color:#ffffff; line-height:1.1; text-shadow:0 6px 22px rgba(0,0,0,0.9); }}
      #cta-chips {{ display:flex; gap:40px; align-items:center; }}
      .chip {{ font-weight:900; font-size:{CHIP_FONT}px; text-transform:uppercase; padding:30px 64px; border-radius:999px; box-shadow:0 16px 46px rgba(0,0,0,0.6); letter-spacing:0.02em; }}
      .chip-a {{ background:#FFD600; color:#05060a; }}
      #cta-follow {{ font-weight:800; font-size:{FOLLOW_FONT}px; color:#b8f1ff; letter-spacing:0.04em; text-transform:uppercase; text-shadow:0 4px 16px rgba(0,0,0,0.85); }}
      #fadeout {{ position:absolute; inset:0; z-index:185; background:#05060a; opacity:0; pointer-events:none; }}
'''

manifest = []
for g in range(N_CHUNKS):
    a, b = bounds[g], bounds[g + 1]
    if a >= b:
        continue
    t0 = CLIPS[a]['start']
    t1 = CLIPS[b]['start'] if b < N else COMP
    CG = round(t1 - t0, 3)
    is_last = (g == N_CHUNKS - 1)

    layers, cfg = [], []
    for j, c in enumerate(CLIPS[a:b]):
        st = round(max(0.0, c['start'] - t0), 3)
        d = c['dur']
        layers.append(
            f'      <div id="L{j}" class="layer" style="z-index:{10+j}"><div class="zoom" id="Z{j}" data-layout-allow-overflow>'
            f'<video id="v{j}" class="clip" data-start="{st}" data-duration="{d}" data-media-start="{MS}" '
            f'data-track-index="{j}" src="assets/clips/{c["file"]}" muted playsinline crossorigin="anonymous"></video></div></div>')
        cfg.append(f"{{s:{st},d:{d}}}")
    layers_html = "\n".join(layers)
    clip_cfg = ",\n        ".join(cfg)

    caps = []
    for cap in CAPS:
        if t0 <= cap['start'] < t1:
            nc = dict(cap)
            nc['start'] = round(cap['start'] - t0, 3)
            nc['end'] = round(cap['end'] - t0, 3)
            nc['disp_end'] = round(min(cap['disp_end'] - t0, CG), 3)
            caps.append(nc)
    caps_json = json.dumps(caps, separators=(",", ":"))
    flashes = [round(f - t0, 3) for f in FLASHES if t0 <= f < t1]
    flash_json = json.dumps(flashes)

    cta_block, cta_js = "", ""
    if t0 <= CTA_AT < t1:
        ca = round(CTA_AT - t0, 3)
        pre_html = f'\n        <div id="cta-pre">{CTA["pre"]}</div>' if CTA.get('pre') else ""
        pre_js = f'\n      tl.fromTo("#cta-pre",{{y:40,opacity:0}},{{y:0,opacity:1,duration:0.5,ease:"power3.out"}}, {ca}+0.1);' if CTA.get('pre') else ""
        cta_block = f'''      <div id="cta">
        <div id="cta-bg"></div>{pre_html}
        <div id="cta-q">{CTA['q']}</div>
        <div id="cta-chips"><div class="chip chip-a">{CTA['chip']}</div></div>
        <div id="cta-follow">{CTA['follow']}</div>
      </div>'''
        cta_js = f'''
      tl.set("#cta",{{visibility:"visible"}}, {ca});
      tl.fromTo("#cta",{{opacity:0}},{{opacity:1,duration:0.5,ease:"power2.out"}}, {ca});{pre_js}
      tl.fromTo("#cta-q",{{y:46,opacity:0}},{{y:0,opacity:1,duration:0.55,ease:"power3.out"}}, {ca}+0.35);
      tl.fromTo(".chip-a",{{scale:0.6,opacity:0}},{{scale:1,opacity:1,duration:0.5,ease:"back.out(1.8)"}}, {ca}+0.7);
      tl.fromTo("#cta-follow",{{y:26,opacity:0}},{{y:0,opacity:1,duration:0.5,ease:"power2.out"}}, {ca}+1.05);
      tl.to(".chip-a",{{scale:1.05,duration:0.5,ease:"sine.inOut",repeat:5,yoyo:true}}, {ca}+1.8);'''

    fade_js = f'\n      tl.to("#fadeout",{{opacity:1,duration:0.7,ease:"power2.in"}}, {round(CG-0.7,3)});' if is_last else ""
    prog_from = round(t0 / COMP, 5); prog_to = round(t1 / COMP, 5)

    html = f'''<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" /><meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Mind Unlocked — chunk {g}</title>
    <script src="https://cdn.jsdelivr.net/npm/gsap@3.14.2/dist/gsap.min.js"></script>
    <style>{STYLE}    </style>
  </head>
  <body>
    <div id="root" data-composition-id="main" data-width="{W}" data-height="{H}" data-start="0" data-duration="{CG}">
{layers_html}

      <div id="grade"></div>
      <div id="tint"></div>
      <div id="vignette"></div>
      <div id="cap"></div>
      <div id="progwrap"><div id="prog"></div></div>
      <div id="flash"></div>
{cta_block}
      <div id="fadeout"></div>
    </div>
    <script>
      window.__timelines = window.__timelines || {{}};
      const tl = gsap.timeline({{ paused: true }});
      const CLIPS = [
        {clip_cfg}
      ];
      CLIPS.forEach(function(c,i){{
        if(i>0){{ tl.fromTo("#L"+i,{{opacity:0}},{{opacity:1,duration:0.35,ease:"sine.inOut"}}, c.s); }}
        tl.fromTo("#Z"+i,{{scale:1.0}},{{scale:1.06,duration:c.d,ease:"none"}}, c.s);
      }});
      const CAPS = {caps_json};
      const capLayer = document.getElementById("cap");
      const fit = (window.__hyperframes && window.__hyperframes.fitTextFontSize) ? window.__hyperframes.fitTextFontSize : null;
      CAPS.forEach(function(g,i){{
        const el = document.createElement("div"); el.className="cg"; el.id="cg"+i;
        const text = g.words.map(function(w){{return w.t;}}).join(" ");
        g.words.forEach(function(w,wi){{
          const sp=document.createElement("span");
          sp.textContent=w.t+(wi<g.words.length-1?" ":"");
          if(w.hl) sp.className="hl";
          el.appendChild(sp);
        }});
        capLayer.appendChild(el);
        if(fit){{ try{{ const r=fit(text.toUpperCase(),{{fontFamily:"Montserrat",fontWeight:800,maxWidth:{CAP_MAXW},baseFontSize:{CAP_FONT},minFontSize:{CAP_MIN},step:2}}); el.style.fontSize=r.fontSize+"px"; }}catch(e){{}} }}
      }});
      CAPS.forEach(function(g,i){{
        const el=document.getElementById("cg"+i); const t=Math.max(0,g.start);
        tl.set(el,{{visibility:"visible"}}, Math.max(0,t-0.02));
        tl.fromTo(el,{{opacity:0,scale:0.78}},{{opacity:1,scale:1.0,duration:0.15,ease:"back.out(2)",overwrite:"auto"}}, t);
        tl.to(el,{{opacity:0,scale:0.95,duration:0.1,ease:"power2.in",overwrite:"auto"}}, Math.max(t+0.1,g.disp_end-0.1));
        tl.set(el,{{visibility:"hidden"}}, g.disp_end);
      }});
      tl.fromTo("#prog",{{scaleX:{prog_from}}},{{scaleX:{prog_to},duration:{CG},ease:"none"}},0);
      {flash_json}.forEach(function(t){{
        tl.to("#flash",{{opacity:0.34,duration:0.06,ease:"power2.out",overwrite:"auto"}}, t);
        tl.to("#flash",{{opacity:0,duration:0.28,ease:"power2.in",overwrite:"auto"}}, t+0.06);
      }});{cta_js}{fade_js}
      window.__timelines["main"] = tl;
    </script>
  </body>
</html>
'''
    fn = f"chunk{g}.html"
    open(fn, 'w', encoding='utf-8').write(html)
    manifest.append({"idx": g, "file": fn, "dur": CG, "clips": b - a, "t0": round(t0, 3), "t1": round(t1, 3)})
    print(f"{fn}: clips {a}..{b} ({b-a}), window {round(t0,1)}-{round(t1,1)}s, dur {CG}s, ~{int(CG*30)} frames")

json.dump(manifest, open('_chunks.json', 'w'), indent=1)
tot = round(sum(m['dur'] for m in manifest), 2)
print(f"\n{len(manifest)} chunks, total {tot}s (target {COMP}s)")
assert abs(tot - COMP) < 0.05, f"chunk durations {tot} != comp {COMP}"
