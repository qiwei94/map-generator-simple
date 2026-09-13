"""Read-only, per-run geometry checks. Never promote fixture tests to acceptance."""
from collections import Counter

VERSION = 'geometry-inspection-v1'


def build_geometry_inspection(layers, meshes=None):
    from aesthetic.surface_grounding import grounding_digest
    from _TEXTURE_STYLE_OF_DEEPSEEK.prepared_surface import mesh_digest
    surface = getattr(layers, 'surface_plan_evidence', {}) or {}
    grounding = getattr(layers, 'surface_grounding', {}) or {}
    checks = []

    def add(key, title, stage, status, scope, evidence, reason):
        checks.append(dict(id=key, title_zh=title, stage=stage, status=status,
                           scope_zh=scope, evidence=evidence, reason_zh=reason))

    for role, mesh_role, title in (('city', 'block_base', '街块与普通建筑'),
                                   ('landmarks', 'landmarks', '地标建筑')):
        plan = grounding.get(role)
        declared = (surface.get('grounding') or {}).get(role, {})
        if plan is None:
            add(f'{role}.grounding', f'{title}：整面贴地', 'S8', 'pending',
                '冻结地形上的几何接地', {}, '本次未携带接地计划，不能用旧质心平挤出证明代替。')
            continue
        valid_plan = (plan.get('fingerprint') == grounding_digest(plan)
                      == declared.get('fingerprint'))
        add(f'{role}.plan', f'{title}：接地计划完整性', 'S6',
            'passed' if valid_plan else 'failed', '计划内容与 S6 指纹一致',
            dict(fingerprint=plan.get('fingerprint'), terrain_fingerprint=plan.get('terrain_fingerprint'),
                 polygon_count=len(plan['patches'])), '计划通过不等于实体已生成或通过切片。')
        add(f'{role}.strategy', f'{title}：坡差与屋顶抬高', 'S6', 'measured',
            '造型测量；不设未经验证的审美合格阈值',
            {k: declared.get(k) for k in ('draped_polygons', 'flat_roof_polygons',
                'max_underfoot_relief_mm', 'max_roof_raise_vs_centroid_mm')},
            '水平屋顶可能导致下坡侧基础较高，需斜视图复核。')
        count = len(plan['patches'])
        mesh = meshes.get(mesh_role) if meshes is not None else None
        proof = (mesh.metadata.get('surface_materialization') or {}) if mesh is not None else {}
        bound = bool(valid_plan and mesh is not None and proof.get('passed') is True
            and proof.get('grounding_fingerprint') == plan['fingerprint']
            and proof.get('input_geometry_fingerprint') == plan['input_geometry_fingerprint']
            and proof.get('mesh_sha256') == mesh_digest(mesh)
            and proof.get('verified_polygons') == count and proof.get('lost_polygons') == 0)
        if not count:
            status = 'failed' if mesh is not None or not valid_plan else 'not_applicable'
        elif meshes is None:
            status = 'pending'
        else:
            status = 'passed' if bound else 'failed'
        add(f'{role}.materialization', f'{title}：实体与冻结分片一致', 'S8', status,
            '未做水体布尔裁切的冻结地形；不等于最终组合接触',
            dict(approved_polygons=count, verified_polygons=proof.get('verified_polygons'),
                 lost_polygons=proof.get('lost_polygons'), mesh_sha256=proof.get('mesh_sha256')),
            '核对实际网格摘要、计划指纹与数量；缺失或被改动的网格不会显示通过。')
        shell_ok = bool(bound and mesh.is_watertight and mesh.is_winding_consistent and mesh.volume > 0)
        shell_status = ('passed' if shell_ok else 'failed') if status == 'passed' else status
        add(f'{role}.shell', f'{title}：闭合、绕向与正体积', 'S8', shell_status,
            '几何完整性，不是打印可行性的充分条件',
            dict(watertight=bool(mesh.is_watertight) if mesh is not None else None,
                 volume_mm3=float(mesh.volume) if mesh is not None else None),
            '固定厚度与平屋顶体积按不同规则验证，不能混用平挤出公式。')
    road = surface.get('road_surface_plan') or {}
    road_ground = grounding.get('roads')
    if road_ground is None:
        # The legacy flat road proof must never turn these checks green.
        for key, title, count in (
                ('road_contact', '贴地道路的坡面接触', road.get('polygon_count')),
                ('bridge_support', '桥面与两岸／引道衔接', road.get('bridge_source_line_parts'))):
            add(key, title, 'S8', 'not_applicable' if count == 0 else 'pending',
                '道路与桥面独立规则', dict(source_or_surface_count=count),
                '本次没有道路接地计划；不能靠旧平挤出证明通过。')
    else:
        declared = (surface.get('grounding') or {}).get('roads') or {}
        valid_plan = (grounding_digest(road_ground) == road_ground.get('fingerprint') == declared.get('fingerprint'))
        support = road_ground.get('support_evidence', [])
        blocked = not valid_plan or any(e.get('status') != 'ready' for e in support)
        add('road_plan', '道路与桥面：接地计划／支撑条件', 'S6',
            'failed' if blocked else 'passed', '源线与两岸几何条件；不是实际跨桥打印验收',
            dict(bridge_support=support, base_offset_mm=declared.get('base_offset_mm'),
                 height_mm=declared.get('height_mm')), '弯桥、岸点不足或横坡过大时阻断，不删除桥梁或回退河床取高。')
        mesh = meshes.get('roads') if meshes is not None else None
        proof = (mesh.metadata.get('surface_materialization') or {}) if mesh is not None else {}
        bound = bool(not blocked and mesh is not None and proof.get('passed') is True
            and proof.get('grounding_fingerprint') == road_ground.get('fingerprint')
            and proof.get('mesh_sha256') == mesh_digest(mesh)
            and proof.get('verified_polygons') == declared.get('polygon_count')
            and proof.get('lost_polygons') == 0 and mesh.is_watertight and mesh.is_winding_consistent)
        for key, title, count in (
                ('road_contact', '贴地道路的坡面接触', declared.get('ordinary_road_count', 0)),
                ('bridge_support', '桥面与两岸／引道衔接', declared.get('bridge_polygon_count', 0))):
            status = ('failed' if blocked else 'not_applicable') if not count else (
                'failed' if blocked else 'pending' if meshes is None else 'passed' if bound else 'failed')
            add(key, title, 'S8', status,
                '冻结地形道路／源线桥岸界面；水体布尔裁切后另验',
                dict(polygon_count=count, mesh_sha256=proof.get('mesh_sha256')),
                '通过表示本次实体与独立接地计划绑定，不代表最终水体接触或无支撑跨桥可打印。')
    if meshes is not None and meshes.get('terrain') is not None and road_ground is not None:
        from aesthetic.final_bank_contact import inspect_bank_contact
        try:
            bank = inspect_bank_contact(layers, meshes)
        except Exception as exc:
            bank = dict(status='failed', reason_zh=str(exc))
        add('final_bank_contact', '水体处理后桥头接触抽检', 'S9', bank['status'],
            '实际桥面与最终地形，两岸中心邻域实体相交；非完整岸线覆盖', bank,
            '独立抽检不能替代整个模型的接触、切片与打印验收。')
    add('final_contact', '水体裁切后的最终组合接触', 'S9', 'pending',
        '最终实体的悬空、埋入、断缝及覆盖', {}, '冻结分片一致不能证明布尔操作后的接触。')
    add('slice_survival', '切片后的缝隙与高度细节存活', 'S11', 'pending',
        '本次最终文件与实际打印配置', {}, '小样回归或其他城市的切片不能冒充本次任务验收。')
    counts = dict(Counter(c['status'] for c in checks))
    return dict(version=VERSION, title_zh='几何完整性与接地检测',
        status='error' if counts.get('failed') else 'partial',
        checks=checks, counts=counts, print_acceptance='pending',
        summary_zh='分别记录已测量、通过、失败、待检测和不适用；单项通过不代表整模型可打印。')
