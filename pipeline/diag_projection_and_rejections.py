import pandas as pd
import numpy as np

# 1. Windowed projection & segments inspection
segs = pd.read_parquet('data/processed/segments.parquet')
print('=== 1. WINDOWED PROJECTION STATISTICS ===')
print(f'Total segments: {len(segs):,}')
print(f'Distinct shapes: {segs["shape_id"].nunique():,}')
print('\nSnap Error From (m) Percentiles:')
print(segs['snap_err_from_m'].describe(percentiles=[0.25, 0.5, 0.75, 0.9, 0.95, 0.99]))
print('\nSnap Error To (m) Percentiles:')
print(segs['snap_err_to_m'].describe(percentiles=[0.25, 0.5, 0.75, 0.9, 0.95, 0.99]))

# Monotonicity check
non_mono = (segs['start_m'] >= segs['end_m']).sum()
print(f'\nNon-monotonic segments (start_m >= end_m): {non_mono}')

# Inter-stop continuity along shapes
discont_count = 0
for sid, grp in segs.groupby('shape_id'):
    starts = grp['start_m'].values[1:]
    ends = grp['end_m'].values[:-1]
    gap = np.abs(starts - ends)
    if np.any(gap > 100.0):
        discont_count += 1
pct_discont = (discont_count / segs['shape_id'].nunique()) * 100.0
print(f'Shapes with >100m inter-stop gap/discontinuity: {discont_count} ({pct_discont:.2f}%)')

print(f'Segments with length == 20.0m: {(segs["length_m"] == 20.0).sum():,}')
print(f'Segments with length <= 25.0m: {(segs["length_m"] <= 25.0).sum():,}')

# 2. Maximum segments investigation
print('\n=== 2. INVESTIGATION OF MAXIMUM SEGMENTS (>= 10km) ===')
max_segs = segs.sort_values('length_m', ascending=False).head(10)
stops = pd.read_parquet('data/processed/stops_utm.parquet').set_index('stop_id')
rs = pd.read_parquet('data/processed/route_shapes.parquet')
routes = pd.read_csv('data/gtfs_static/routes.txt', dtype={'route_id': str}).set_index('route_id')

for _, row in max_segs.iterrows():
    f_name = stops.loc[row['from_stop_id'], 'stop_name'] if row['from_stop_id'] in stops.index else 'Unknown'
    t_name = stops.loc[row['to_stop_id'], 'stop_name'] if row['to_stop_id'] in stops.index else 'Unknown'
    r_matches = rs[rs['shape_id'] == row['shape_id']]['route_id'].tolist()
    r_names = [routes.loc[r, 'route_short_name'] if r in routes.index else r for r in r_matches]
    speed_kmh = (row['length_m'] / max(row['sched_sec'], 1.0)) * 3.6
    print(f"Segment: {row['segment_id']} | Shape: {row['shape_id']} | Routes: {r_names}")
    print(f"  From: {f_name} ({row['from_stop_id']}) -> To: {t_name} ({row['to_stop_id']})")
    print(f"  Length: {row['length_m']:.1f} m | Sched Sec: {row['sched_sec']:.1f}s ({row['sched_sec']/60:.1f} min) | Speed: {speed_kmh:.1f} km/h")
    print(f"  Start_m: {row['start_m']:.1f} m | End_m: {row['end_m']:.1f} m | Support Trips: {row['trip_support']}")

# 3. Day 1 Rejections inspection
print('\n=== 3. DAY 1 (OCT 01, 2026) REJECTION DISTRIBUTIONS ===')
val_obs = pd.read_parquet('data/processed/daily_segment_obs/bus_movements_2026_10_01.parquet')
rej_obs = pd.read_parquet('data/processed/rejected_obs/bus_movements_2026_10_01_rejected.parquet')

print(f'Valid count: {len(val_obs):,} | Rejected count: {len(rej_obs):,}')
print('\nRejection Reason Breakdown:')
print(rej_obs['reject_reason'].value_counts())

print('\nRejection Distribution by Hour of Day:')
hourly_rej = pd.crosstab(rej_obs['hour'], rej_obs['reject_reason'])
hourly_rej['Total_Rej'] = hourly_rej.sum(axis=1)
valid_by_hr = val_obs['hour'].value_counts()
hourly_rej['Valid'] = valid_by_hr
hourly_rej['Total_Obs'] = hourly_rej['Total_Rej'] + hourly_rej['Valid']
hourly_rej['Rej_Rate_Pct'] = (hourly_rej['Total_Rej'] / hourly_rej['Total_Obs']) * 100.0
print(hourly_rej[['Total_Rej', 'Valid', 'Rej_Rate_Pct', 'PING_GAP_TOO_LARGE', 'SPEED_TOO_HIGH', 'DT_TOO_SMALL', 'SPEED_TOO_LOW']].round(1))

# Route-level rejections
rej_with_shape = rej_obs.merge(segs[['segment_id', 'shape_id']], on='segment_id')
rej_with_route = rej_with_shape.merge(rs[['shape_id', 'route_id']], on='shape_id')
rej_with_route['route_name'] = rej_with_route['route_id'].map(routes['route_short_name'])

print('\nTop 10 Routes by Rejection Volume:')
top_rej_routes = rej_with_route['route_name'].value_counts().head(10)
print(top_rej_routes)
