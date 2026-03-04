import json
import gspread
from google.oauth2.service_account import Credentials
from openai import OpenAI

# --- Setup ---
scopes = ["https://www.googleapis.com/auth/spreadsheets",
          "https://www.googleapis.com/auth/drive"]
creds  = Credentials.from_service_account_file("credentials.json", scopes=scopes)
client = gspread.authorize(creds)

client_ai = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key="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",
)

# --- Read all rows from sheet ---
# Columns: time | temp | humidity | light1 (up) | light2 (right) | light3 (left)
# Area label rows: standalone rows where first cell has area name, no numeric temp
sheet = client.open_by_key("1Id_Xqca_o_gnzt7mx754nGBuUQ43yt5CxtrS5wtz54Q").sheet1
all_values = sheet.get_all_values()

if len(all_values) < 2:
    print("Not enough data yet!")
    exit()

# --- Parse rows into areas ---
areas       = {}   # { "area 1": [ [time, temp, hum, l1, l2, l3], ... ] }
area_labels = {}   # { "area 1": "Area 1" }  — preserves original casing / custom name

current_area = None

for row in all_values[1:]:   # skip header
    if not any(cell.strip() for cell in row):
        continue  # skip fully empty rows

    first = row[0].strip()

    # Decide if this is a label row: second cell is empty or non-numeric
    second_is_numeric = False
    if len(row) > 1 and row[1].strip():
        try:
            float(row[1])
            second_is_numeric = True
        except ValueError:
            pass

    is_label = first and not second_is_numeric

    if is_label:
        key = first.lower()
        current_area = key
        if current_area not in areas:
            areas[current_area] = []
            area_labels[current_area] = first
        continue

    
    if current_area and len(row) >= 6:
        try:
            areas[current_area].append([
                row[0].strip(),   # time
                float(row[1]),    # temp
                float(row[2]),    # humidity
                float(row[3]),    # light1 up
                float(row[4]),    # light2 right
                float(row[5]),    # light3 left
            ])
        except ValueError:
            continue

# Drop areas with no readings silently
areas = {k: v for k, v in areas.items() if v}

if not areas:
    print("No area data found!")
    exit()

print(f"Found {len(areas)} areas with data: {[area_labels[k] for k in areas]}")


# ================================================================
# SECTION 1 — TEMP & HUMIDITY ANALYSIS
# ================================================================
print("\n--- Analyzing Temp & Humidity ---")
th_results = []

for area_key, readings in areas.items():
    label = area_labels[area_key]
    temps  = [r[1] for r in readings]
    humids = [r[2] for r in readings]
    t0, t1 = readings[0][0], readings[-1][0]

    avg_t = round(sum(temps)  / len(temps),  2)
    avg_h = round(sum(humids) / len(humids), 2)
    max_t = max(temps);  min_t = min(temps)
    max_h = max(humids); min_h = min(humids)

    readings_text = "\n".join(
        f"  R{i+1}: {r[0]} | {r[1]}°C | {r[2]}%"
        for i, r in enumerate(readings)
    )

    prompt = f"""Analyze temperature and humidity sensor data from {label.upper()}.
{len(readings)} readings from {t0} to {t1}.

Readings (time | temp | humidity):
{readings_text}

Stats:
Avg Temp {avg_t}°C, Max {max_t}°C, Min {min_t}°C
Avg Humidity {avg_h}%, Max {max_h}%, Min {min_h}%

Assess: stability, anomalies, and overall heat/humidity risk.

Return ONLY valid JSON, no markdown, no extra text:
{{
  "area": "{label}",
  "time_range": "{t0} to {t1}",
  "num_readings": {len(readings)},
  "avg_temp": {avg_t},
  "avg_humidity": {avg_h},
  "max_temp": {max_t},
  "max_humidity": {max_h},
  "stability": "stable or moderate or unstable",
  "anomalies": "description or None",
  "analysis": "2-3 sentence assessment",
  "risk_score": <integer 1 to 10>
}}"""

    print(f"  {label}...")
    resp = client_ai.chat.completions.create(
        model="google/gemini-2.5-pro-preview",
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.choices[0].message.content.strip().replace("```json","").replace("```","").strip()
    try:
        th_results.append(json.loads(raw))
    except json.JSONDecodeError as e:
        print(f"  Parse error ({label}): {e}")

# Rank temp/humidity
if len(th_results) > 1:
    summary = "\n".join(
        f"  {a['area'].upper()}: AvgTemp={a['avg_temp']}°C MaxTemp={a['max_temp']}°C "
        f"AvgHumidity={a['avg_humidity']}% MaxHumidity={a['max_humidity']}% Risk={a['risk_score']}/10"
        for a in th_results
    )
    rp = f"""Rank {len(th_results)} areas by thermal/humidity concern.
Rank 1 = highest concern, rank {len(th_results)} = lowest.
Priority order: high temperature > high humidity > combinations.

{summary}

Return ONLY a JSON array, no extra text:
[{{"area": "exact name", "rank": 1, "rank_reason": "short comparison reason"}}]"""

    print("  Ranking areas...")
    rr = client_ai.chat.completions.create(
        model="google/gemini-2.5-pro-preview",
        messages=[{"role": "user", "content": rp}],
    )
    rr_raw = rr.choices[0].message.content.strip().replace("```json","").replace("```","").strip()
    try:
        rank_map = {r["area"].lower(): r for r in json.loads(rr_raw)}
        for a in th_results:
            m = rank_map.get(a["area"].lower())
            a["rank"]        = m["rank"]        if m else 99
            a["rank_reason"] = m["rank_reason"] if m else "Not ranked"
    except Exception as e:
        print(f"  Ranking error: {e}")
        for i, a in enumerate(th_results):
            a["rank"] = i + 1; a["rank_reason"] = "Auto-ranked"
elif th_results:
    th_results[0]["rank"] = 1
    th_results[0]["rank_reason"] = "Only area analyzed"

th_results.sort(key=lambda x: x.get("rank", 99))


# ================================================================
# SECTION 2 — LIGHT SENSOR ANALYSIS
# ================================================================
print("\n--- Analyzing Light Sensors ---")
light_results = []

for area_key, readings in areas.items():
    label = area_labels[area_key]
    l1s = [r[3] for r in readings]
    l2s = [r[4] for r in readings]
    l3s = [r[5] for r in readings]
    t0, t1 = readings[0][0], readings[-1][0]

    a1 = round(sum(l1s)/len(l1s), 2)
    a2 = round(sum(l2s)/len(l2s), 2)
    a3 = round(sum(l3s)/len(l3s), 2)
    avg_all = round((a1+a2+a3)/3, 2)

    readings_text = "\n".join(
        f"  R{i+1}: {r[0]} | L1={r[3]}% L2={r[4]}% L3={r[5]}%"
        for i, r in enumerate(readings)
    )

    # Pre-compute converted light values for display (100 - darkness%)
    c1 = round(100 - a1, 2)
    c2 = round(100 - a2, 2)
    c3 = round(100 - a3, 2)
    c_avg = round((c1 + c2 + c3) / 3, 2)
    converted_readings_text = "\n".join(
        f"  R{i+1}: {r[0]} | L1={round(100-r[3],1)}% L2={round(100-r[4],1)}% L3={round(100-r[5],1)}%"
        for i, r in enumerate(readings)
    )

    prompt = f"""You are analyzing outdoor NIGHT LIGHT POLLUTION sensor data from {label.upper()}.

IMPORTANT — RAW VALUES ARE DARKNESS PERCENTAGES:
The sensor outputs a darkness percentage (0% = fully bright, 100% = fully dark).
You MUST convert every value to a light/pollution reading using: light% = 100 - darkness%
This has already been done for you in the converted readings below. Use ONLY the converted values for your analysis.

Sensor directions:
- L1 = Up (sky-facing): detects overhead skyglow and upward light scatter
- L2 = Right: detects lateral light bleed from the right
- L3 = Left: detects lateral light bleed from the left

{len(readings)} readings from {t0} to {t1}.

Converted readings — light pollution % (already = 100 - raw darkness%):
{converted_readings_text}

Converted averages: L1 Up={c1}%  L2 Right={c2}%  L3 Left={c3}%  Overall={c_avg}%
(Higher converted value = more light pollution = worse)

Assess this area from a light pollution perspective:
1. Overall pollution severity — how much artificial light is present at night?
2. Is the dominant source skyglow (L1 high) or lateral bleed from surroundings (L2/L3 high)?
3. Are readings stable (constant source like streetlights) or intermittent (vehicles, signage)?
4. What these readings likely indicate about the location (e.g. open sky near city, sheltered alley, near bright signage, under highway lighting)
5. Recommendations to reduce or shield against light pollution in this area

Return ONLY valid JSON, no markdown, no extra text.
Use the CONVERTED light pollution values (not raw darkness values) in the JSON fields:
{{
  "area": "{label}",
  "time_range": "{t0} to {t1}",
  "num_readings": {len(readings)},
  "avg_light1": {c1},
  "avg_light2": {c2},
  "avg_light3": {c3},
  "avg_overall": {c_avg},
  "pollution_level": "severe or high or moderate or low",
  "dominant_source": "skyglow (L1) or right-side bleed (L2) or left-side bleed (L3) or mixed",
  "stability": "stable or intermittent or variable",
  "source_inference": "1-2 sentences on what the readings suggest about the pollution source and location character",
  "analysis": "2-3 sentence light pollution assessment — use terms like skyglow, light bleed, artificial light intrusion",
  "recommendations": "specific actionable steps to reduce or mitigate light pollution at this location"
}}"""

    print(f"  {label}...")
    resp = client_ai.chat.completions.create(
        model="google/gemini-2.5-pro-preview",
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.choices[0].message.content.strip().replace("```json","").replace("```","").strip()
    try:
        light_results.append(json.loads(raw))
    except json.JSONDecodeError as e:
        print(f"  Parse error ({label}): {e}")

# Rank by light pollution severity (most polluted = rank 1)
if len(light_results) > 1:
    lsummary = "\n".join(
        f"  {a['area'].upper()}: Overall={a['avg_overall']}% "
        f"L1={a['avg_light1']}% L2={a['avg_light2']}% L3={a['avg_light3']}% "
        f"PollutionLevel={a['pollution_level']} DominantSource={a['dominant_source']}"
        for a in light_results
    )
    lrp = f"""Rank {len(light_results)} areas by night light pollution severity.
Rank 1 = most polluted (highest artificial light, most concern), rank {len(light_results)} = least polluted (darkest, least concern).
Higher sensor readings = more pollution = higher rank number concern.

{lsummary}

Return ONLY a JSON array, no extra text:
[{{"area": "exact name", "rank": 1, "rank_reason": "short reason comparing pollution levels across areas"}}]"""

    print("  Ranking light areas...")
    lr = client_ai.chat.completions.create(
        model="google/gemini-2.5-pro-preview",
        messages=[{"role": "user", "content": lrp}],
    )
    lr_raw = lr.choices[0].message.content.strip().replace("```json","").replace("```","").strip()
    try:
        lrank_map = {r["area"].lower(): r for r in json.loads(lr_raw)}
        for a in light_results:
            m = lrank_map.get(a["area"].lower())
            a["rank"]        = m["rank"]        if m else 99
            a["rank_reason"] = m["rank_reason"] if m else "Not ranked"
    except Exception as e:
        print(f"  Light ranking error: {e}")
        for i, a in enumerate(light_results):
            a["rank"] = i + 1; a["rank_reason"] = "Auto-ranked"
elif light_results:
    light_results[0]["rank"] = 1
    light_results[0]["rank_reason"] = "Only area analyzed"

light_results.sort(key=lambda x: x.get("rank", 99))


# ================================================================
# HTML GENERATION
# ================================================================
total_th    = len(th_results)
total_light = len(light_results)

def rank_color(rank, total):
    if total <= 1: return "#ff4444"
    ratio = 1 - ((rank - 1) / (total - 1))
    if ratio >= 0.8:   return "#ff4444"
    elif ratio >= 0.6: return "#ff8800"
    elif ratio >= 0.4: return "#ffcc00"
    elif ratio >= 0.2: return "#88cc00"
    else:              return "#44bb44"

def pill(text, bg):
    return (f'<span style="background:{bg};color:#000;padding:2px 9px;border-radius:12px;'
            f'font-size:0.76em;font-weight:bold;white-space:nowrap;">{text.upper()}</span>')

def stability_pill(s):
    c = {"stable":"#44bb44","moderate":"#ffcc00","unstable":"#ff4444"}.get((s or "").lower(),"#888")
    return pill(s or "unknown", c)

def pollution_pill(s):
    c = {"severe":"#ff4444","high":"#ff8800","moderate":"#ffcc00","low":"#44bb44"}.get((s or "").lower(),"#888")
    return pill(s or "unknown", c)

def stability_pill2(s):
    c = {"stable":"#88aaff","intermittent":"#ffcc00","variable":"#ff8800"}.get((s or "").lower(),"#888")
    return pill(s or "unknown", c)

def sensor_bar(label, value, scale):
    pct = min(int((value / max(scale, 1)) * 100), 100)
    # For light pollution: high = red (bad), low = green (good)
    bc  = "#ff4444" if pct >= 60 else "#ffcc00" if pct >= 35 else "#44bb44"
    return f"""<div style="margin-bottom:9px;">
        <div style="display:flex;justify-content:space-between;font-size:0.79em;color:#aaa;margin-bottom:3px;">
            <span>{label}</span><span style="color:#fff;font-weight:bold;">{value}%</span>
        </div>
        <div style="background:#333;border-radius:4px;height:7px;">
            <div style="background:{bc};width:{pct}%;height:7px;border-radius:4px;"></div>
        </div>
    </div>"""

LIGHT_LEGEND = """<div class="legend">
    <span style="color:#ff4444;">🔴 #1 Most polluted</span>
    <span style="color:#ff8800;">🟠 High pollution</span>
    <span style="color:#ffcc00;">🟡 Moderate</span>
    <span style="color:#88cc00;">🟢 Low pollution</span>
    <span style="color:#44bb44;">💚 Least polluted</span>
</div>"""

# --- Temp/Humidity cards ---
th_cards = ""
for z in th_results:
    rank  = z.get("rank", 1)
    color = rank_color(rank, total_th)
    th_cards += f"""
<div class="area-card" style="border-left:6px solid {color};">
  <div class="card-header" style="background:{color}18;border-bottom:1px solid {color}30;">
    <div class="rank-badge" style="background:{color};color:#000;">#{rank}</div>
    <div class="area-title">{z['area'].upper()}</div>
    <div style="flex:1;"></div>
    {stability_pill(z.get('stability',''))}
  </div>
  <div style="padding:3px 16px 0;font-size:0.76em;color:#555;">{z.get('time_range','')}</div>
  <div class="card-body">
    <div class="metrics">
      <div class="metric">
        <span class="metric-label">🌡️ Avg Temp</span>
        <span class="metric-value" style="color:#ff8888;">{z['avg_temp']}°C</span>
        <span class="metric-sub">Max: {z['max_temp']}°C</span>
      </div>
      <div class="metric">
        <span class="metric-label">💧 Avg Humidity</span>
        <span class="metric-value" style="color:#88aaff;">{z['avg_humidity']}%</span>
        <span class="metric-sub">Max: {z['max_humidity']}%</span>
      </div>
      <div class="metric">
        <span class="metric-label">⚠️ Risk Score</span>
        <span class="metric-value" style="color:{color};">{z.get('risk_score','?')}/10</span>
        <span class="metric-sub">{z.get('num_readings','?')} readings</span>
      </div>
    </div>
    <div class="analysis-box">
      <div class="asec"><strong>📊 Analysis</strong><p>{z.get('analysis','N/A')}</p></div>
      <div class="asec"><strong>🔍 Anomalies</strong><p>{z.get('anomalies','None')}</p></div>
      <div class="asec" style="border-left:3px solid {color};padding-left:10px;">
        <strong>🏆 Why this rank</strong><p>{z.get('rank_reason','N/A')}</p>
      </div>
    </div>
  </div>
</div>"""

# --- Light cards ---
light_cards = ""
for z in light_results:
    rank  = z.get("rank", 1)
    color = rank_color(rank, total_light)
    scale = max(z['avg_light1'], z['avg_light2'], z['avg_light3'], 10) * 1.3
    light_cards += f"""
<div class="area-card" style="border-left:6px solid {color};">
  <div class="card-header" style="background:{color}18;border-bottom:1px solid {color}30;">
    <div class="rank-badge" style="background:{color};color:#000;">#{rank}</div>
    <div class="area-title">{z['area'].upper()}</div>
    <div style="flex:1;"></div>
    {pollution_pill(z.get('pollution_level',''))}
    &nbsp;{stability_pill2(z.get('stability',''))}
  </div>
  <div style="padding:3px 16px 0;font-size:0.76em;color:#555;">{z.get('time_range','')}</div>
  <div class="card-body">
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:12px;">
      <div>
        <div style="font-size:0.76em;color:#666;margin-bottom:10px;text-transform:uppercase;letter-spacing:1px;">Sensor Readings (higher = more pollution)</div>
        {sensor_bar("🌌 L1 — Up / Skyglow", z['avg_light1'], scale)}
        {sensor_bar("➡️ L2 — Right bleed", z['avg_light2'], scale)}
        {sensor_bar("⬅️ L3 — Left bleed", z['avg_light3'], scale)}
        <div style="font-size:0.78em;color:#666;margin-top:10px;">
          Overall avg: <strong style="color:#fff;">{z['avg_overall']}%</strong>
          &nbsp;·&nbsp; Dominant: <strong style="color:#ffaa55;">{z.get('dominant_source','?')}</strong>
        </div>
      </div>
      <div style="display:flex;flex-direction:column;gap:8px;">
        <div class="asec" style="flex:1;"><strong>📊 Pollution Assessment</strong><p>{z.get('analysis','N/A')}</p></div>
        <div class="asec" style="flex:1;"><strong>🔍 Likely Source</strong><p>{z.get('source_inference','N/A')}</p></div>
      </div>
    </div>
    <div class="asec" style="border-left:3px solid {color};padding-left:10px;">
      <strong>🛡️ Mitigation Recommendations</strong><p>{z.get('recommendations','N/A')}</p>
    </div>
    <div class="asec" style="margin-top:8px;">
      <strong>🏆 Why this rank</strong><p>{z.get('rank_reason','N/A')}</p>
    </div>
  </div>
</div>"""

TH_LEGEND = """<div class="legend">
    <span style="color:#ff4444;">🔴 #1 Highest concern</span>
    <span style="color:#ff8800;">🟠 High</span>
    <span style="color:#ffcc00;">🟡 Moderate</span>
    <span style="color:#88cc00;">🟢 Low</span>
    <span style="color:#44bb44;">💚 Lowest concern</span>
</div>"""

html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>ESP32 Zone Dashboard</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0;}}
  body{{font-family:Arial,sans-serif;background:#0f0f0f;color:#eee;padding:30px;max-width:960px;margin:0 auto;}}
  h1{{color:#fff;font-size:1.55em;margin-bottom:4px;}}
  .subtitle{{color:#555;font-size:0.86em;margin-bottom:30px;}}
  .section-title{{font-size:1.15em;font-weight:bold;color:#fff;margin:36px 0 5px;
    padding-bottom:8px;border-bottom:2px solid #2a2a2a;}}
  .section-desc{{color:#666;font-size:0.83em;margin-bottom:14px;}}
  .legend{{display:flex;gap:16px;flex-wrap:wrap;font-size:0.82em;margin-bottom:18px;}}
  .area-card{{background:#1a1a1a;border-radius:8px;margin-bottom:16px;overflow:hidden;}}
  .card-header{{display:flex;align-items:center;gap:12px;padding:11px 16px;flex-wrap:wrap;}}
  .rank-badge{{font-size:1.25em;font-weight:bold;padding:3px 11px;border-radius:20px;white-space:nowrap;}}
  .area-title{{font-size:1.05em;font-weight:bold;color:#fff;}}
  .card-body{{padding:12px 16px 16px;}}
  .metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(115px,1fr));gap:10px;margin-bottom:12px;}}
  .metric{{background:#222;border-radius:6px;padding:9px;text-align:center;}}
  .metric-label{{display:block;font-size:0.76em;color:#aaa;margin-bottom:3px;}}
  .metric-value{{display:block;font-size:1.3em;font-weight:bold;}}
  .metric-sub{{display:block;font-size:0.7em;color:#666;margin-top:2px;}}
  .analysis-box{{display:flex;flex-direction:column;gap:8px;}}
  .asec{{background:#222;border-radius:6px;padding:9px 11px;}}
  .asec strong{{color:#aaa;font-size:0.79em;display:block;margin-bottom:4px;text-transform:uppercase;letter-spacing:0.5px;}}
  .asec p{{color:#bbb;font-size:0.84em;line-height:1.55;}}
  hr{{border:none;border-top:1px solid #1e1e1e;margin:40px 0;}}
</style>
</head>
<body>

<h1>🚗 Environmental Zone Dashboard</h1>
<p class="subtitle">Areas analyzed independently — thermal risk and lighting quality ranked separately. Rank #1 = highest concern.</p>

<div class="section-title">🌡️ Temperature &amp; Humidity Priority</div>
<p class="section-desc">Ranked by heat and humidity risk. Rank #1 = most dangerous thermal conditions.</p>
{TH_LEGEND}
{th_cards}

<hr>

<div class="section-title">🌃 Night Light Pollution Assessment &amp; Priority</div>
<p class="section-desc">Ranked by light pollution severity. Rank #1 = most polluted area. Higher sensor readings = more artificial light intrusion. Bars show L1 skyglow (up) · L2 right bleed · L3 left bleed — red = high pollution, green = low.</p>
{LIGHT_LEGEND}
{light_cards}

</body>
</html>"""

with open("dashboard.html", "w", encoding="utf-8") as f:
    f.write(html)

print(f"\n✅ Dashboard generated!")
print(f"   Temp/Humidity: {len(th_results)} areas ranked")
print(f"   Light:         {len(light_results)} areas ranked")
print("   Open dashboard.html in your browser.")
