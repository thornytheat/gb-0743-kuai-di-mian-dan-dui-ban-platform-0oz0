"""逐包裹发运放行修订。

修订只读取已归档批次的同一份回执快照，不覆盖旧版清单：
模板（templates）、坐标（coordinates）、遮挡（occlusion）、条码（barcode）
与作废关系（tickets/voids）全部取自当前批次，证据随每条裁决一起下发。
"""

KEEP = 'KEEP'
REISSUE = 'REISSUE'
HOLD = 'HOLD'

ACTIONS = {KEEP: '沿用', REISSUE: '换新面单', HOLD: '暂缓'}


def _index(rows, key):
    return {row[key]: row for row in rows}


def _label_evidence(label, coordinates, barcode, occlusion, templates, ledger):
    """把同一面单在模板、坐标、遮挡、条码、作废链上的依据汇成一份证据。"""
    coord = coordinates.get(label)
    scan = barcode.get(label)
    layout = occlusion.get(label)
    binding = next((row for row in templates['bindings'] if row['label'] == label), None)
    ticket = ledger['labels_by_label'].get(label)
    return {
        'label': label,
        'template': None if not binding or binding['line'] is None else {
            'format': templates['versions_by_line'].get(binding['line'], {}).get('format'),
            'revision': binding['revision'],
            'version_line': binding['line'],
        },
        'coordinate': None if not coord or 'code' in coord else {
            'device': coord['device'],
            'dpi': coord['dpi'],
            'rotation': coord['rotation'],
            'origin': coord['origin'],
            'unit': coord['unit'],
            'paper': coord['paper'],
            'boxes': coord['boxes'],
        },
        'coordinate_code': coord.get('code') if coord and 'code' in coord else None,
        'barcode': None if scan is None else {
            'ok': scan['ok'],
            'checks': scan['checks'],
        },
        'occlusion': None if layout is None else {
            'ok': layout['ok'],
            'quiet_mm': layout['quiet_mm'],
            'issues': layout['issues'],
        },
        'chain': None if ticket is None else {
            'state': ticket['state'],
            'replaces': ticket['replaces'],
            'replaced_by': ticket['replaced_by'],
            'codes': list(ticket['codes']),
            'void': ticket['void'],
        },
    }


def _decide_parcel(parcel, live_labels, ticket_rows, evidence_for):
    """逐包裹裁决：一张可发单沿用；作废待接换新；隔离或无单暂缓。"""
    rows_by_label = {row['label']: row for row in ticket_rows}
    live = [rows_by_label[label] for label in live_labels if label in rows_by_label]
    if live:
        # 勾稽保证每包裹至多一张有效面单，多张属于勾稽异常，按暂缓处理。
        if len(live) > 1:
            action, reason = HOLD, 'MULTIPLE_LIVE'
        else:
            action, reason = KEEP, 'LIVE_LABEL'
        decided = live
    else:
        pending = [row for row in ticket_rows if row['state'] == 'revoked' and not row['replaced_by']]
        blocked = [row for row in ticket_rows if row['state'] == 'isolated']
        if pending:
            action, reason = REISSUE, 'VOID_AWAITING_REPRINT'
            decided = pending
        elif blocked:
            action, reason = HOLD, 'ISOLATED_LABEL'
            decided = blocked
        else:
            action, reason = HOLD, 'NO_LABEL'
            decided = ticket_rows
    return {
        'parcel': parcel,
        'action': action,
        'action_label': ACTIONS[action],
        'reason': reason,
        'live_labels': live_labels,
        'labels': [row['label'] for row in ticket_rows],
        'evidence': [evidence_for(row['label']) for row in decided],
    }


def build_release(snapshot):
    """从单个批次快照构造逐包裹放行修订（纯函数，不写入、不重算）。"""
    templates = snapshot['templates']
    coordinates = _index(snapshot['coordinates'], 'label')
    barcode = _index(snapshot['barcode'], 'label')
    occlusion = _index(snapshot['occlusion'], 'label')
    tickets = snapshot['tickets']
    ledger = {
        'labels_by_label': _index(tickets['labels'], 'label'),
        'labels': tickets['labels'],
    }
    templates = dict(templates, versions_by_line=_index(templates['versions'], 'line'))

    def evidence_for(label):
        return _label_evidence(label, coordinates, barcode, occlusion, templates, ledger)

    by_parcel = {}
    for row in tickets['labels']:
        by_parcel.setdefault(row['parcel'], []).append(row)

    decisions = []
    for parcel in snapshot['reconcile']['parcels']:
        key = parcel['parcel']
        rows = by_parcel.get(key, [])
        decisions.append(_decide_parcel(key, list(parcel['live_labels']), rows, evidence_for))

    counts = {action: sum(item['action'] == action for item in decisions) for action in ACTIONS}
    return {
        'run_id': snapshot['manifest']['id'],
        'rule_version': snapshot['manifest']['rule_version'],
        'inputs': snapshot['manifest']['inputs'],
        'decisions': decisions,
        'counts': counts,
        'totals': {
            'parcels': snapshot['reconcile']['parcels_total'],
            'shippable': snapshot['reconcile']['shippable'],
            'unissued': snapshot['reconcile']['unissued'],
        },
        'voids': {
            'applied': tickets['applied'],
            'excluded': tickets['excluded_voids'],
        },
        # 隔离的出单回执（坏行、冲突、重复）同样来自同批回执，保留原值不计数。
        'excluded_prints': snapshot['intake']['excluded'],
    }
