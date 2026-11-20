import copy

from app.modules.intake.reader import append_row
from app.modules.release.service import ACTIONS, HOLD, KEEP, REISSUE, build_release
from app.modules.tickets.pipeline import run


def decisions(batch):
    return {row['parcel']: row for row in build_release(batch)['decisions']}


def test_seeded_batch_decides_keep_reissue_and_hold_per_parcel(batch):
    result = build_release(batch)
    table = decisions(batch)
    # 有效面单 → 沿用；只裁决当前可发单，不动同包裹的隔离/作废历史。
    assert table['0003']['action'] == KEEP
    assert table['0003']['live_labels'] == ['L-003']
    # L-001 已作废并由 L-001-R 接续 → 沿用新单，旧单仍保留在链上。
    one = table['0001']
    assert one['action'] == KEEP and one['live_labels'] == ['L-001-R']
    assert set(one['labels']) == {'L-001', 'L-001-R', 'L-001-B'}
    # L-015 已作废且无后继 → 换新面单。
    assert table['0015']['action'] == REISSUE
    assert table['0015']['reason'] == 'VOID_AWAITING_REPRINT'
    # 物理/对读缺陷、无模板、未知包裹 → 暂缓。
    for parcel, code in [('0005', 'ISOLATED_LABEL'), ('0009', 'ISOLATED_LABEL'),
                         ('0013', 'ISOLATED_LABEL'), ('0099', 'ISOLATED_LABEL')]:
        assert table[parcel]['action'] == HOLD and table[parcel]['reason'] == code
    # 台账里完全没有出单的包裹 → 暂缓且无标签。
    assert table['0024']['action'] == HOLD and table['0024']['labels'] == []
    counts = result['counts']
    # 决策覆盖勾稽里的全部包裹（含台账外的未知包裹 0099，共 29 个键）。
    assert sum(counts.values()) == len(result['decisions'])
    assert counts[KEEP] == result['totals']['shippable'] == 15
    assert counts[REISSUE] == 1 and counts[HOLD] == 13
    assert set(result['decisions'][0]) >= {'action', 'action_label', 'reason', 'evidence'}


def test_each_decision_carries_template_coordinate_occlusion_barcode_and_void_evidence(batch):
    table = decisions(batch)
    proof = table['0001']['evidence'][0]
    assert proof['label'] == 'L-001-R'
    # 五类依据全部来自同一批次快照。
    assert proof['template']['revision'] in (1, 2)
    assert list(proof['coordinate']['boxes']['barcode'].values()) == ['20', '20', '60', '14']
    assert proof['barcode']['ok'] is True
    assert proof['occlusion']['ok'] is True
    assert proof['chain']['state'] == 'valid' and proof['chain']['replaces'] == 'L-001'
    # 换新裁决携带的是作废旧单及其作废登记证据。
    pending = table['0015']['evidence'][0]
    assert pending['label'] == 'L-015' and pending['chain']['state'] == 'revoked'
    assert pending['chain']['void']['id'] == 'A-02' and pending['chain']['void']['reason'] == 'SMEAR'
    # 暂缓裁决保留缺陷原因码，供复核组核对，不重写面单。
    blocked = table['0006']['evidence'][0]
    assert blocked['chain']['codes'] == ['FIELD_OUTSIDE_ZONE', 'FIELD_OVERLAP', 'QUIET_OCCUPIED']
    issue_codes = {item['code'] for item in blocked['occlusion']['issues']}
    assert issue_codes == {'FIELD_OUTSIDE_ZONE', 'FIELD_OVERLAP', 'QUIET_OCCUPIED'}


def test_release_is_a_read_only_view_that_never_mutates_the_batch(batch):
    before = copy.deepcopy(batch)
    build_release(batch)
    assert batch == before


def test_multiple_live_labels_force_a_hold_until_reconciled(batch):
    broken = copy.deepcopy(batch)
    # 人为制造勾稽异常：同一包裹出现两张有效面单。
    old = next(row for row in broken['tickets']['labels'] if row['label'] == 'L-001')
    old['state'] = 'valid'
    parcel = next(row for row in broken['reconcile']['parcels'] if row['parcel'] == '0001')
    parcel['live_labels'] = ['L-001', 'L-001-R']
    decision = next(row for row in build_release(broken)['decisions'] if row['parcel'] == '0001')
    assert decision['action'] == HOLD and decision['reason'] == 'MULTIPLE_LIVE'
    assert {proof['label'] for proof in decision['evidence']} == {'L-001', 'L-001-R'}


def test_late_reprint_moves_parcel_from_reissue_to_keep_without_touching_others(batch, sources):
    before = decisions(batch)
    assert before['0015']['action'] == REISSUE
    new_print = {"label": "L-015-NEW", "parcel": "0015", "at": "2026-11-20T08:38:00+08:00", "format": "CN100",
                 "device": "P-01", "dpi": 300, "rotation": 0, "origin": "TL", "unit": "mm",
                 "fields": {"barcode": {"box": [20, 20, 60, 14], "value": "00078263100015"},
                            "route": {"box": [12, 46, 30, 7], "value": "E-03"},
                            "recipient": {"box": [12, 70, 68, 9], "value": "R-015"}},
                 "replaces": "L-015"}
    rerun = run(dict(sources, prints=append_row(sources['prints'], new_print)))
    after = decisions(rerun)
    assert after['0015']['action'] == KEEP and after['0015']['live_labels'] == ['L-015-NEW']
    # 其余包裹裁决与可发面单保持不变，避免整批重签改掉没问题的包裹。
    for parcel in before:
        if parcel != '0015':
            assert after[parcel]['action'] == before[parcel]['action']
            assert after[parcel]['live_labels'] == before[parcel]['live_labels']
    assert rerun['reconcile']['shippable'] == 16


def test_duplicate_late_reprint_is_counted_once(sources):
    new_print = {"label": "L-015-NEW", "parcel": "0015", "at": "2026-11-20T08:38:00+08:00", "format": "CN100",
                 "device": "P-01", "dpi": 300, "rotation": 0, "origin": "TL", "unit": "mm",
                 "fields": {"barcode": {"box": [20, 20, 60, 14], "value": "00078263100015"},
                            "route": {"box": [12, 46, 30, 7], "value": "E-03"},
                            "recipient": {"box": [12, 70, 68, 9], "value": "R-015"}},
                 "replaces": "L-015"}
    raw = append_row(append_row(sources['prints'], new_print), new_print)
    rerun = run(dict(sources, prints=raw))
    result = build_release(rerun)
    table = {row['parcel']: row for row in result['decisions']}
    # 晚到记录重跑：精确重复只采信一条，包裹仍只得到一张可发面单。
    assert table['0015']['live_labels'] == ['L-015-NEW']
    assert result['counts'][KEEP] == 16
    # 仅新增的第 41 行被记为重复，样例原有的 L-025 重复不受影响。
    duplicate = [row for row in rerun['intake']['excluded']
                 if row['code'] == 'DUPLICATE_PRINT' and row['line'] >= 40]
    assert len(duplicate) == 1 and duplicate[0]['data']['label'] == 'L-015-NEW'


def test_duplicate_late_void_is_never_double_counted(batch, sources):
    late_void = {"id": "A-03", "label": "L-005", "at": "2026-11-20T08:42:00+08:00", "reason": "LOST"}
    raw = append_row(append_row(sources['voids'], late_void), late_void)
    rerun = run(dict(sources, voids=raw))
    result = build_release(rerun)
    # A-03 因目标面单本就隔离而无法作废，且同编号重报全部隔离：
    # 已应用作废始终只有 A-01/A-02，A-03 绝不进入可发链或被重复计数。
    applied_ids = {row['id'] for row in result['voids']['applied']}
    assert applied_ids == {'A-01', 'A-02'}
    late = [row for row in result['voids']['excluded']
            if row['data']['id'] == 'A-03' and row['line'] >= 4]
    assert len(late) == 2 and all(row['code'] == 'DUPLICATE_VOID' for row in late)
    assert {row['parcel']: row['action'] for row in result['decisions']} == \
           {row['parcel']: row['action'] for row in build_release(batch)['decisions']}


def test_failed_revision_keeps_the_archived_batch_and_original_evidence(client, batch_id, sources):
    # 触发一次会失败的重打（时间早于作废），旧批次指针与四份原始证据必须原样保留。
    bad = {'expected': batch_id, 'target': 'L-015', 'label': 'L-015-NEW',
           'at': '2026-11-20T08:36:00+08:00'}
    response = client.post('/api/tickets/reprint', json=bad)
    assert response.status_code == 422 and response.json['code'] == 'REPRINT_TIME'
    assert client.get('/health').json['run'] == batch_id
    db = client.application.extensions['batch_store']
    with db.transaction() as tx:
        assert tx.execute('SELECT count(*) FROM batch').fetchone() == (1,)
        assert tx.execute('SELECT count(*) FROM source').fetchone() == (4,)
    # 放行修订仍基于未被覆盖的旧版清单生成。
    release = client.get('/api/release').json
    assert release['id'] == batch_id
    table = {row['parcel']: row for row in release['data']['decisions']}
    assert table['0015']['action'] == REISSUE


def test_release_endpoint_supports_archived_runs_and_exposes_actions(client, batch_id):
    response = client.get(f'/api/release?run={batch_id}')
    assert response.status_code == 200
    body = response.json
    assert body['id'] == batch_id and body['actions'] == ACTIONS
    assert body['data']['run_id'] == batch_id
    assert {row['action'] for row in body['data']['decisions']} <= set(ACTIONS)
    # 未知批次返回 404，不产生新批次。
    assert client.get('/api/release?run=not-a-run').status_code == 404


def test_release_page_is_registered_in_the_workbench(client):
    html = client.get('/').get_data(as_text=True)
    assert '发运放行修订' in html and 'stage-release' in html
