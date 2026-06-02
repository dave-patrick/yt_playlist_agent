import json
with open('yt_category_channel_map.txt', 'r', encoding='utf-8') as f:
    channel_map = {}
    for line in f:
        if ':' in line:
            parts = line.strip().split(':')
            if len(parts) == 2:
                channel_map[parts[0].strip().lower()] = parts[1].strip()

def get_cat(channel):
    channel_lower = channel.lower()
    if channel_lower in channel_map:
        return channel_map[channel_lower]
    for ch_key, cat in channel_map.items():
        if ch_key in channel_lower:
            return cat
    return None

anomalies = {}
with open('scratch/Star_Wars_titles.txt', 'r', encoding='utf-8') as f:
    for line in f:
        if ':' in line:
            channel = line.split(':')[0].strip()
            cat = get_cat(channel)
            if cat and cat != 'Star Wars':
                anomalies[channel] = cat

if anomalies:
    print("Channels in Star Wars playlist that are mapped elsewhere:")
    for ch, cat in anomalies.items():
        print(f"  - {ch} (Mapped to {cat})")
else:
    print("No mapped anomalies found in Star Wars.")
