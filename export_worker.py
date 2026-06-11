import argparse
import json
import os
import sys

import bpy


class ExportWorkerError(Exception):
    """Headless exportを明確なエラーメッセージ付きで中断するための例外。"""


def log(message):
    """UI側が逐次表示できるよう、Headless処理ログを即時flushする。"""
    print("[NDBE] " + str(message), flush=True)


def fail(message):
    """UI側がエラー要約を拾える形式で失敗を出力する。"""
    print("NDBE_ERROR: " + str(message), flush=True)


def parse_args(argv):
    """Blender引数の`--`以降からworker専用引数を取り出す。"""
    if "--" in argv:
        argv = argv[argv.index("--") + 1 :]
    else:
        argv = []
    parser = argparse.ArgumentParser(description="Headless FBX export worker")
    parser.add_argument("--job-json", required=True)
    return parser.parse_args(argv)


def load_job(path):
    """UI側が書き出したジョブJSONを読み込む。"""
    if not os.path.exists(path):
        raise ExportWorkerError("Job JSON was not found: " + path)
    with open(path, "r", encoding="utf-8") as file:
        return json.load(file)


def get_object(name, expected_type=None, required=True):
    """名前でオブジェクトを取得し、型の食い違いを早期に検出する。"""
    if not name:
        if required:
            raise ExportWorkerError("Required object name is empty.")
        return None
    obj = bpy.data.objects.get(name)
    if not obj:
        if required:
            raise ExportWorkerError("Object was not found: " + name)
        return None
    if expected_type and obj.type != expected_type:
        raise ExportWorkerError(name + " is not a " + expected_type + " object.")
    return obj


def ensure_object_mode():
    """現在のactive状態に依存せず、可能な場合だけObject Modeへ戻す。"""
    if bpy.ops.object.mode_set.poll():
        bpy.ops.object.mode_set(mode="OBJECT")


def make_active(obj):
    """Blender operatorが対象を取り違えないよう、選択とactiveを明示する。"""
    ensure_object_mode()
    bpy.ops.object.select_all(action="DESELECT")
    obj.hide_viewport = False
    obj.hide_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def set_action_pose(armature, action_name, frame):
    """指定Actionの指定フレームを現在ポーズとして評価する。"""
    if not armature:
        return
    if not action_name:
        raise ExportWorkerError("Action is required when armature is set.")
    action = bpy.data.actions.get(action_name)
    if not action:
        raise ExportWorkerError("Action was not found: " + action_name)
    armature.animation_data_create()
    armature.animation_data.action = action
    bpy.context.scene.frame_set(int(frame))
    bpy.context.view_layer.update()
    log("Applied action pose: " + action_name + " frame " + str(frame))


def non_armature_modifier_names(obj):
    """Armature以外で、Viewport評価対象のモディファイアだけを適用対象にする。"""
    return [modifier.name for modifier in obj.modifiers if modifier.type != "ARMATURE" and modifier.show_viewport]


def capture_evaluated_mesh(obj):
    """現在のShape Key値とモディファイア評価後のメッシュを複製する。"""
    bpy.context.view_layer.update()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = obj.evaluated_get(depsgraph)
    return bpy.data.meshes.new_from_object(evaluated, depsgraph=depsgraph)


def set_all_shape_key_values(obj, value):
    """評価対象のShape Keyを1つずつ切り替えるため、全値を一旦そろえる。"""
    if not obj.data.shape_keys:
        return
    for key_block in obj.data.shape_keys.key_blocks:
        key_block.value = value


def collect_shape_key_specs(obj):
    """再構築後にUI上のShape Key設定をできるだけ戻すためのメタデータを保持する。"""
    specs = []
    for key_block in obj.data.shape_keys.key_blocks:
        specs.append(
            {
                "name": key_block.name,
                "value": key_block.value,
                "slider_min": key_block.slider_min,
                "slider_max": key_block.slider_max,
                "mute": key_block.mute,
                "interpolation": key_block.interpolation,
                "vertex_group": key_block.vertex_group,
                "relative_key": key_block.relative_key.name if key_block.relative_key else "",
            }
        )
    return specs


def disable_armature_modifiers(obj):
    """非Armatureモディファイア適用時にスキニングを焼き込まないよう一時的に無効化する。"""
    states = {}
    for modifier in obj.modifiers:
        if modifier.type == "ARMATURE":
            states[modifier.name] = modifier.show_viewport
            modifier.show_viewport = False
    return states


def restore_armature_modifiers(obj, states):
    """一時的に無効化したArmatureモディファイアの表示状態を戻す。"""
    for name, show_viewport in states.items():
        modifier = obj.modifiers.get(name)
        if modifier:
            modifier.show_viewport = show_viewport


def apply_modifiers_without_shape_keys(obj, modifier_names):
    """Shape Keyが無いメッシュではBlender標準のmodifier_applyを使う。"""
    if not modifier_names:
        return
    make_active(obj)
    armature_states = disable_armature_modifiers(obj)
    try:
        for name in modifier_names:
            if obj.modifiers.get(name):
                bpy.ops.object.modifier_apply(modifier=name)
    finally:
        restore_armature_modifiers(obj, armature_states)


def apply_modifiers_preserve_shape_keys(obj):
    """Shape Keyを各キーごとに評価し、モディファイア適用後のキーとして再構築する。"""
    modifier_names = non_armature_modifier_names(obj)
    if not modifier_names:
        return

    if not obj.data.shape_keys:
        apply_modifiers_without_shape_keys(obj, modifier_names)
        log("Applied modifiers: " + obj.name)
        return

    make_active(obj)
    armature_states = disable_armature_modifiers(obj)
    old_mesh = obj.data
    key_specs = collect_shape_key_specs(obj)
    original_values = [key.value for key in old_mesh.shape_keys.key_blocks]
    generated_meshes = []

    try:
        # Basisと各Shape Keyを同じモディファイア状態で評価し、頂点数一致を検証する。
        for index, key_block in enumerate(old_mesh.shape_keys.key_blocks):
            set_all_shape_key_values(obj, 0.0)
            if index > 0:
                key_block.value = 1.0
            generated_meshes.append(capture_evaluated_mesh(obj))

        vertex_count = len(generated_meshes[0].vertices)
        for mesh in generated_meshes:
            if len(mesh.vertices) != vertex_count:
                raise ExportWorkerError("Shape Key vertex count mismatch on " + obj.name)

        basis_mesh = generated_meshes[0]
        obj.data = basis_mesh

        for name in modifier_names:
            modifier = obj.modifiers.get(name)
            if modifier:
                obj.modifiers.remove(modifier)

        key_map = {}
        basis_key = obj.shape_key_add(name=key_specs[0]["name"])
        key_map[basis_key.name] = basis_key

        for spec, mesh in zip(key_specs[1:], generated_meshes[1:]):
            shape_key = obj.shape_key_add(name=spec["name"])
            key_map[shape_key.name] = shape_key
            for vertex_index, vertex in enumerate(mesh.vertices):
                shape_key.data[vertex_index].co = vertex.co

        for spec in key_specs:
            shape_key = key_map.get(spec["name"])
            if not shape_key:
                continue
            shape_key.value = spec["value"]
            shape_key.slider_min = spec["slider_min"]
            shape_key.slider_max = spec["slider_max"]
            shape_key.mute = spec["mute"]
            shape_key.interpolation = spec["interpolation"]
            shape_key.vertex_group = spec["vertex_group"]
            relative_key = key_map.get(spec["relative_key"])
            if relative_key:
                shape_key.relative_key = relative_key

        log("Applied modifiers preserving shape keys: " + obj.name)
    finally:
        restore_armature_modifiers(obj, armature_states)
        if old_mesh.shape_keys:
            for key_block, value in zip(old_mesh.shape_keys.key_blocks, original_values):
                key_block.value = value
        for mesh in generated_meshes[1:]:
            bpy.data.meshes.remove(mesh)


def apply_all_transforms(objects):
    """出力対象のオブジェクト変換をメッシュ/アーマチュアデータ側へ焼き込む。"""
    objects = [obj for obj in objects if obj]
    if not objects:
        return
    ensure_object_mode()
    bpy.ops.object.select_all(action="DESELECT")
    for obj in objects:
        obj.hide_viewport = False
        obj.hide_set(False)
        obj.select_set(True)
    bpy.context.view_layer.objects.active = objects[0]
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True, properties=True)
    log("Applied all transforms.")


def merge_group(group):
    """Blender標準のJoinにより、UVや属性をできるだけ維持したままメッシュを結合する。"""
    output_name = group.get("output_name", "").strip()
    mesh_names = group.get("meshes", [])
    if not output_name:
        raise ExportWorkerError("Merge group output name is empty.")
    if not mesh_names:
        raise ExportWorkerError("Merge group has no mesh objects: " + output_name)

    objects = [get_object(name, "MESH") for name in mesh_names]
    existing = bpy.data.objects.get(output_name)
    if existing and existing not in objects:
        raise ExportWorkerError("Merge output name already exists: " + output_name)

    ensure_object_mode()
    bpy.ops.object.select_all(action="DESELECT")
    for obj in objects:
        obj.hide_viewport = False
        obj.hide_set(False)
        obj.select_set(True)
    bpy.context.view_layer.objects.active = objects[0]

    if len(objects) > 1:
        bpy.ops.object.join()

    merged = bpy.context.view_layer.objects.active
    merged.name = output_name
    merged.data.name = output_name
    log("Merged meshes into: " + output_name)
    return merged


def apply_pose_as_rest(armature):
    """現在ポーズをFBX上のレストポーズにする。"""
    if not armature:
        return
    make_active(armature)
    bpy.ops.object.mode_set(mode="POSE")
    bpy.ops.pose.select_all(action="SELECT")
    try:
        bpy.ops.pose.armature_apply(selected=False)
    except TypeError:
        bpy.ops.pose.armature_apply()
    bpy.ops.object.mode_set(mode="OBJECT")
    log("Applied pose as rest pose: " + armature.name)


def build_target_objects(job):
    """ジョブ設定から前処理対象と最終出力対象を名前ベースで解決する。"""
    armature = get_object(job.get("armature", ""), "ARMATURE", required=False)
    export_mesh_names = list(dict.fromkeys(job.get("export_meshes", [])))
    merge_source_names = []
    for group in job.get("merge_groups", []):
        merge_source_names.extend(group.get("meshes", []))
    all_mesh_names = list(dict.fromkeys(export_mesh_names + merge_source_names))
    meshes = [get_object(name, "MESH") for name in all_mesh_names]
    return armature, meshes


def export_fbx(job, export_objects):
    """選択オブジェクトのみをFBXへ出力する。"""
    output_path = job.get("output_path", "")
    if not output_path:
        raise ExportWorkerError("Output path is empty.")
    output_dir = os.path.dirname(output_path)
    if not os.path.isdir(output_dir):
        raise ExportWorkerError("Output directory does not exist: " + output_dir)

    fbx = job.get("fbx", {})
    object_types = set(fbx.get("object_types", ["MESH", "ARMATURE"]))
    if not object_types:
        raise ExportWorkerError("FBX object_types is empty.")

    ensure_object_mode()
    bpy.ops.object.select_all(action="DESELECT")
    for obj in export_objects:
        obj.hide_viewport = False
        obj.hide_set(False)
        obj.select_set(True)
    if export_objects:
        bpy.context.view_layer.objects.active = export_objects[0]

    bpy.ops.export_scene.fbx(
        filepath=output_path,
        check_existing=False,
        use_selection=True,
        object_types=object_types,
        apply_scale_options=fbx.get("apply_scale_options", "FBX_SCALE_ALL"),
        add_leaf_bones=bool(fbx.get("add_leaf_bones", False)),
        bake_anim=bool(fbx.get("bake_anim", False)),
        use_armature_deform_only=bool(fbx.get("use_armature_deform_only", True)),
    )
    log("Exported FBX: " + output_path)


def run(job):
    """ジョブ全体の前処理とFBX出力を実行する。"""
    log("Starting job: " + job.get("job_name", "Unnamed"))
    armature, meshes = build_target_objects(job)
    set_action_pose(armature, job.get("action", ""), job.get("frame", 1))

    for obj in meshes:
        apply_modifiers_preserve_shape_keys(obj)

    merged_objects = []
    merged_source_names = set()
    for group in job.get("merge_groups", []):
        merged_objects.append(merge_group(group))
        merged_source_names.update(group.get("meshes", []))

    export_mesh_names = [name for name in job.get("export_meshes", []) if name not in merged_source_names]
    export_meshes = [get_object(name, "MESH") for name in export_mesh_names]
    export_meshes.extend(merged_objects)

    transform_targets = []
    if armature:
        transform_targets.append(armature)
    transform_targets.extend(export_meshes)
    apply_all_transforms(transform_targets)
    apply_pose_as_rest(armature)

    fbx_types = set(job.get("fbx", {}).get("object_types", ["MESH", "ARMATURE"]))
    export_objects = []
    if armature and "ARMATURE" in fbx_types:
        export_objects.append(armature)
    if "MESH" in fbx_types:
        export_objects.extend(export_meshes)
    if not export_objects:
        raise ExportWorkerError("No objects selected for export.")

    export_fbx(job, export_objects)


def main():
    """Headless Blenderから呼ばれるエントリーポイント。"""
    args = parse_args(sys.argv)
    try:
        job = load_job(args.job_json)
        run(job)
    except ExportWorkerError as exc:
        fail(exc)
        return 2
    except Exception as exc:
        fail(type(exc).__name__ + ": " + str(exc))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
