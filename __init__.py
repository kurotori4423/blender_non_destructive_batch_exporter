bl_info = {
    "name": "Non-Destructive Batch Exporter",
    "author": "kurotori4423",
    "version": (0, 1, 0),
    "blender": (4, 5, 7),
    "location": "View3D > Sidebar > ND Exporter",
    "description": "Run destructive FBX export preparation in a headless temporary Blender process.",
    "category": "Import-Export",
}

import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading

import bpy


ADDON_ID = "ndbe"


def _enqueue_output(stream, log_queue):
    """Headless Blenderの標準出力をUIスレッドへ渡すための読み取りワーカー。"""
    try:
        for line in iter(stream.readline, ""):
            log_queue.put(line.rstrip("\n"))
    finally:
        stream.close()


def _selected_mesh_objects(context):
    """現在選択されているメッシュだけを設定へ取り込む。"""
    return [obj for obj in context.selected_objects if obj.type == "MESH"]


def _safe_collection_remove(collection, index):
    """UIから渡されたindexが古い場合でもBlenderを例外で止めないようにする。"""
    if 0 <= index < len(collection):
        collection.remove(index)
        return True
    return False


class NDBE_MeshItem(bpy.types.PropertyGroup):
    """メッシュオブジェクト参照をCollectionPropertyへ保存するための項目。"""

    mesh: bpy.props.PointerProperty(
        name="Mesh",
        type=bpy.types.Object,
        poll=lambda self, obj: obj.type == "MESH",
        description="Export target mesh object",
    )


class NDBE_MergeGroup(bpy.types.PropertyGroup):
    """複数メッシュを1つの出力メッシュへまとめる設定。"""

    output_name: bpy.props.StringProperty(
        name="Output Name",
        default="MergedMesh",
        description="Name of the merged output object",
    )
    meshes: bpy.props.CollectionProperty(type=NDBE_MeshItem)
    meshes_index: bpy.props.IntProperty(name="Mesh Index", default=0)
    show_expanded: bpy.props.BoolProperty(name="Show Expanded", default=True)


class NDBE_ExportJob(bpy.types.PropertyGroup):
    """Headless workerへ渡すエクスポートジョブ設定。"""

    name: bpy.props.StringProperty(name="Job Name", default="ExportJob")
    output_dir: bpy.props.StringProperty(name="Output Dir", default="", subtype="DIR_PATH")
    file_name: bpy.props.StringProperty(name="File Name", default="export.fbx")
    show_expanded: bpy.props.BoolProperty(name="Show Expanded", default=True)

    armature: bpy.props.PointerProperty(
        name="Armature",
        type=bpy.types.Object,
        poll=lambda self, obj: obj.type == "ARMATURE",
        description="Armature used for rest pose export",
    )
    action: bpy.props.PointerProperty(
        name="Action",
        type=bpy.types.Action,
        description="Action evaluated as the export rest pose",
    )
    frame: bpy.props.IntProperty(name="Frame", default=1)

    export_meshes: bpy.props.CollectionProperty(type=NDBE_MeshItem)
    export_meshes_index: bpy.props.IntProperty(name="Export Mesh Index", default=0)
    merge_groups: bpy.props.CollectionProperty(type=NDBE_MergeGroup)
    merge_groups_index: bpy.props.IntProperty(name="Merge Group Index", default=0)

    include_meshes: bpy.props.BoolProperty(name="Export Meshes", default=True)
    include_armature: bpy.props.BoolProperty(name="Export Armature", default=True)
    use_armature_deform_only: bpy.props.BoolProperty(name="Deform Bones Only", default=True)
    add_leaf_bones: bpy.props.BoolProperty(name="Add Leaf Bones", default=False)
    bake_anim: bpy.props.BoolProperty(name="Bake Animation", default=False)
    apply_scale_options: bpy.props.EnumProperty(
        name="Apply Scale",
        default="FBX_SCALE_ALL",
        items=[
            ("FBX_SCALE_NONE", "None", "Do not apply scaling"),
            ("FBX_SCALE_UNITS", "Units", "Apply unit scaling"),
            ("FBX_SCALE_CUSTOM", "Custom", "Apply custom scaling"),
            ("FBX_SCALE_ALL", "All", "Apply all scaling"),
        ],
    )

    status: bpy.props.StringProperty(name="Status", default="Idle")
    last_log: bpy.props.StringProperty(name="Last Log", default="")


class NDBE_UL_MeshList(bpy.types.UIList):
    """メッシュ参照リストを3D Viewportサイドバーへ表示する。"""

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {"DEFAULT", "COMPACT"}:
            if item.mesh:
                layout.label(text=item.mesh.name, icon="OUTLINER_OB_MESH", translate=False)
            else:
                layout.label(text="Missing Mesh", icon="ERROR")
        elif self.layout_type == "GRID":
            layout.alignment = "CENTER"
            layout.label(text="", icon="OUTLINER_OB_MESH")


class NDBE_PT_Panel(bpy.types.Panel):
    """非破壊バッチエクスポーターのメインUI。"""

    bl_label = "ND Batch Exporter"
    bl_idname = "NDBE_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "ND Exporter"

    def draw(self, context):
        layout = self.layout
        scene = context.scene

        row = layout.row(align=True)
        row.operator("ndbe.add_job", icon="ADD", text="Add Job")
        if scene.ndbe_is_exporting:
            row.label(text="Exporting...", icon="TIME")

        for job_index, job in enumerate(scene.ndbe_jobs):
            box = layout.box()
            header = box.row(align=True)
            icon = "DISCLOSURE_TRI_DOWN" if job.show_expanded else "DISCLOSURE_TRI_RIGHT"
            header.prop(job, "show_expanded", text="", icon=icon, emboss=False)
            header.prop(job, "name", text="")
            delete_op = header.operator("ndbe.delete_job", icon="TRASH", text="")
            delete_op.index = job_index
            export_op = header.operator("ndbe.start_export", icon="EXPORT", text="")
            export_op.index = job_index

            if not job.show_expanded:
                continue

            box.prop(job, "output_dir")
            box.prop(job, "file_name")
            box.prop(job, "armature")
            box.prop(job, "action")
            box.prop(job, "frame")

            fbx_box = box.box()
            fbx_box.label(text="FBX Settings", icon="SETTINGS")
            fbx_box.prop(job, "apply_scale_options")
            row = fbx_box.row(align=True)
            row.prop(job, "include_meshes")
            row.prop(job, "include_armature")
            row = fbx_box.row(align=True)
            row.prop(job, "use_armature_deform_only")
            row.prop(job, "add_leaf_bones")
            fbx_box.prop(job, "bake_anim")

            mesh_box = box.box()
            row = mesh_box.row(align=True)
            row.label(text="Export Meshes", icon="OUTLINER_OB_MESH")
            set_op = row.operator("ndbe.set_export_meshes", icon="RESTRICT_SELECT_OFF", text="Set Selected")
            set_op.index = job_index
            mesh_box.template_list(
                "NDBE_UL_MeshList",
                "NDBE_UL_ExportMeshes_" + str(job_index),
                job,
                "export_meshes",
                job,
                "export_meshes_index",
            )

            merge_box = box.box()
            row = merge_box.row(align=True)
            row.label(text="Merge Groups", icon="AUTOMERGE_OFF")
            add_op = row.operator("ndbe.add_merge_group", icon="ADD", text="")
            add_op.job_index = job_index

            for group_index, group in enumerate(job.merge_groups):
                group_box = merge_box.box()
                group_header = group_box.row(align=True)
                icon = "DISCLOSURE_TRI_DOWN" if group.show_expanded else "DISCLOSURE_TRI_RIGHT"
                group_header.prop(group, "show_expanded", text="", icon=icon, emboss=False)
                group_header.prop(group, "output_name", text="")
                delete_group_op = group_header.operator("ndbe.delete_merge_group", icon="TRASH", text="")
                delete_group_op.job_index = job_index
                delete_group_op.group_index = group_index

                if not group.show_expanded:
                    continue

                row = group_box.row(align=True)
                set_group_op = row.operator("ndbe.set_merge_group_meshes", icon="RESTRICT_SELECT_OFF", text="Set Selected")
                set_group_op.job_index = job_index
                set_group_op.group_index = group_index
                clear_group_op = row.operator("ndbe.clear_merge_group_meshes", icon="X", text="")
                clear_group_op.job_index = job_index
                clear_group_op.group_index = group_index
                group_box.template_list(
                    "NDBE_UL_MeshList",
                    "NDBE_UL_GroupMeshes_" + str(job_index) + "_" + str(group_index),
                    group,
                    "meshes",
                    group,
                    "meshes_index",
                )

            if job.status:
                box.label(text=job.status, icon="INFO")
            if job.last_log:
                box.label(text=job.last_log[:120], icon="TEXT")


class NDBE_OT_AddJob(bpy.types.Operator):
    """新しいエクスポートジョブを追加する。"""

    bl_idname = "ndbe.add_job"
    bl_label = "Add Export Job"

    def execute(self, context):
        job = context.scene.ndbe_jobs.add()
        job.name = "ExportJob " + str(len(context.scene.ndbe_jobs))
        job.file_name = job.name.replace(" ", "_") + ".fbx"
        context.scene.ndbe_jobs_index = len(context.scene.ndbe_jobs) - 1
        return {"FINISHED"}


class NDBE_OT_DeleteJob(bpy.types.Operator):
    """指定されたエクスポートジョブを削除する。"""

    bl_idname = "ndbe.delete_job"
    bl_label = "Delete Export Job"

    index: bpy.props.IntProperty(options={"HIDDEN"})

    def execute(self, context):
        _safe_collection_remove(context.scene.ndbe_jobs, self.index)
        return {"FINISHED"}


class NDBE_OT_SetExportMeshes(bpy.types.Operator):
    """選択中のメッシュをジョブの出力対象として設定する。"""

    bl_idname = "ndbe.set_export_meshes"
    bl_label = "Set Selected Export Meshes"

    index: bpy.props.IntProperty(options={"HIDDEN"})

    def execute(self, context):
        if not 0 <= self.index < len(context.scene.ndbe_jobs):
            return {"CANCELLED"}
        meshes = _selected_mesh_objects(context)
        if not meshes:
            self.report({"WARNING"}, "No mesh objects selected.")
            return {"CANCELLED"}
        job = context.scene.ndbe_jobs[self.index]
        job.export_meshes.clear()
        for obj in meshes:
            item = job.export_meshes.add()
            item.mesh = obj
        return {"FINISHED"}


class NDBE_OT_AddMergeGroup(bpy.types.Operator):
    """メッシュ結合グループを追加する。"""

    bl_idname = "ndbe.add_merge_group"
    bl_label = "Add Merge Group"

    job_index: bpy.props.IntProperty(options={"HIDDEN"})

    def execute(self, context):
        if not 0 <= self.job_index < len(context.scene.ndbe_jobs):
            return {"CANCELLED"}
        job = context.scene.ndbe_jobs[self.job_index]
        group = job.merge_groups.add()
        group.output_name = "MergedMesh " + str(len(job.merge_groups))
        job.merge_groups_index = len(job.merge_groups) - 1
        return {"FINISHED"}


class NDBE_OT_DeleteMergeGroup(bpy.types.Operator):
    """メッシュ結合グループを削除する。"""

    bl_idname = "ndbe.delete_merge_group"
    bl_label = "Delete Merge Group"

    job_index: bpy.props.IntProperty(options={"HIDDEN"})
    group_index: bpy.props.IntProperty(options={"HIDDEN"})

    def execute(self, context):
        if not 0 <= self.job_index < len(context.scene.ndbe_jobs):
            return {"CANCELLED"}
        _safe_collection_remove(context.scene.ndbe_jobs[self.job_index].merge_groups, self.group_index)
        return {"FINISHED"}


class NDBE_OT_SetMergeGroupMeshes(bpy.types.Operator):
    """選択中のメッシュを結合グループの対象として設定する。"""

    bl_idname = "ndbe.set_merge_group_meshes"
    bl_label = "Set Selected Merge Meshes"

    job_index: bpy.props.IntProperty(options={"HIDDEN"})
    group_index: bpy.props.IntProperty(options={"HIDDEN"})

    def execute(self, context):
        if not 0 <= self.job_index < len(context.scene.ndbe_jobs):
            return {"CANCELLED"}
        job = context.scene.ndbe_jobs[self.job_index]
        if not 0 <= self.group_index < len(job.merge_groups):
            return {"CANCELLED"}
        meshes = _selected_mesh_objects(context)
        if not meshes:
            self.report({"WARNING"}, "No mesh objects selected.")
            return {"CANCELLED"}
        group = job.merge_groups[self.group_index]
        group.meshes.clear()
        for obj in meshes:
            item = group.meshes.add()
            item.mesh = obj
        return {"FINISHED"}


class NDBE_OT_ClearMergeGroupMeshes(bpy.types.Operator):
    """結合グループのメッシュ一覧を空にする。"""

    bl_idname = "ndbe.clear_merge_group_meshes"
    bl_label = "Clear Merge Group Meshes"

    job_index: bpy.props.IntProperty(options={"HIDDEN"})
    group_index: bpy.props.IntProperty(options={"HIDDEN"})

    def execute(self, context):
        if not 0 <= self.job_index < len(context.scene.ndbe_jobs):
            return {"CANCELLED"}
        job = context.scene.ndbe_jobs[self.job_index]
        if not 0 <= self.group_index < len(job.merge_groups):
            return {"CANCELLED"}
        job.merge_groups[self.group_index].meshes.clear()
        return {"FINISHED"}


def _serialize_job(context, job):
    """Headless workerがUIに依存せず再現できるよう、名前ベースのJSONへ変換する。"""
    output_dir = bpy.path.abspath(job.output_dir)
    file_name = job.file_name.strip() or (job.name.strip() + ".fbx")
    if not file_name.lower().endswith(".fbx"):
        file_name += ".fbx"

    merge_groups = []
    for group in job.merge_groups:
        merge_groups.append(
            {
                "output_name": group.output_name.strip(),
                "meshes": [item.mesh.name for item in group.meshes if item.mesh],
            }
        )

    object_types = []
    if job.include_meshes:
        object_types.append("MESH")
    if job.include_armature:
        object_types.append("ARMATURE")

    return {
        "job_name": job.name,
        "output_path": os.path.join(output_dir, file_name),
        "armature": job.armature.name if job.armature else "",
        "action": job.action.name if job.action else "",
        "frame": job.frame,
        "export_meshes": [item.mesh.name for item in job.export_meshes if item.mesh],
        "merge_groups": merge_groups,
        "fbx": {
            "object_types": object_types,
            "apply_scale_options": job.apply_scale_options,
            "add_leaf_bones": job.add_leaf_bones,
            "bake_anim": job.bake_anim,
            "use_armature_deform_only": job.use_armature_deform_only,
        },
    }


class NDBE_OT_StartExport(bpy.types.Operator):
    """一時コピーしたblendをHeadless Blenderで開いてFBXを書き出す。"""

    bl_idname = "ndbe.start_export"
    bl_label = "Start Headless Export"

    index: bpy.props.IntProperty(options={"HIDDEN"})

    _process = None
    _timer = None
    _temp_dir = ""
    _log_queue = None
    _reader_thread = None
    _last_error = ""

    def execute(self, context):
        scene = context.scene
        if scene.ndbe_is_exporting:
            self.report({"WARNING"}, "Another export is already running.")
            return {"CANCELLED"}
        if not 0 <= self.index < len(scene.ndbe_jobs):
            return {"CANCELLED"}

        job = scene.ndbe_jobs[self.index]
        if not bpy.data.filepath:
            self.report({"ERROR"}, "Save the blend file before exporting.")
            return {"CANCELLED"}
        if getattr(bpy.data, "is_dirty", False):
            self.report({"ERROR"}, "Save current changes before exporting.")
            return {"CANCELLED"}
        if not bpy.app.binary_path or not os.path.exists(bpy.app.binary_path):
            self.report({"ERROR"}, "Blender executable path was not found.")
            return {"CANCELLED"}
        output_dir = bpy.path.abspath(job.output_dir)
        if not output_dir or not os.path.isdir(output_dir):
            self.report({"ERROR"}, "Output directory does not exist.")
            return {"CANCELLED"}
        if job.armature and not job.action:
            self.report({"ERROR"}, "Action is required when an armature is set.")
            return {"CANCELLED"}

        addon_dir = os.path.dirname(os.path.abspath(__file__))
        worker_path = os.path.join(addon_dir, "export_worker.py")
        if not os.path.exists(worker_path):
            self.report({"ERROR"}, "export_worker.py was not found.")
            return {"CANCELLED"}

        self._temp_dir = tempfile.mkdtemp(prefix="ndbe_")
        source_blend = os.path.join(self._temp_dir, "source.blend")
        job_json_path = os.path.join(self._temp_dir, "job.json")
        shutil.copy2(bpy.data.filepath, source_blend)

        with open(job_json_path, "w", encoding="utf-8") as file:
            json.dump(_serialize_job(context, job), file, ensure_ascii=False, indent=2)

        command = [
            bpy.app.binary_path,
            "--background",
            source_blend,
            "--python",
            worker_path,
            "--",
            "--job-json",
            job_json_path,
        ]

        creationflags = 0
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            creationflags = subprocess.CREATE_NO_WINDOW

        self._log_queue = queue.Queue()
        self._last_error = ""
        try:
            self._process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=addon_dir,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
            )
        except OSError as exc:
            shutil.rmtree(self._temp_dir, ignore_errors=True)
            self.report({"ERROR"}, "Failed to start Blender: " + str(exc))
            return {"CANCELLED"}

        self._reader_thread = threading.Thread(target=_enqueue_output, args=(self._process.stdout, self._log_queue))
        self._reader_thread.daemon = True
        self._reader_thread.start()

        job.status = "Running headless export..."
        job.last_log = ""
        scene.ndbe_is_exporting = True
        self._timer = context.window_manager.event_timer_add(0.25, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if event.type == "ESC":
            if self._process and self._process.poll() is None:
                self._process.terminate()
            return self._finish(context, cancelled=True)

        if event.type != "TIMER":
            return {"PASS_THROUGH"}

        self._drain_logs(context)
        if self._process and self._process.poll() is not None:
            self._drain_logs(context)
            return self._finish(context, cancelled=False)
        return {"PASS_THROUGH"}

    def _drain_logs(self, context):
        """workerログを少しずつUIへ反映し、最後のエラー行を保持する。"""
        if not self._log_queue or not 0 <= self.index < len(context.scene.ndbe_jobs):
            return
        job = context.scene.ndbe_jobs[self.index]
        while True:
            try:
                line = self._log_queue.get_nowait()
            except queue.Empty:
                break
            if not line:
                continue
            job.last_log = line
            if "NDBE_ERROR:" in line:
                self._last_error = line.split("NDBE_ERROR:", 1)[1].strip()

    def _finish(self, context, cancelled):
        """Headlessプロセス終了後のUI状態と一時ファイルを片付ける。"""
        if self._timer:
            context.window_manager.event_timer_remove(self._timer)
        context.scene.ndbe_is_exporting = False

        return_code = self._process.returncode if self._process else 1
        if 0 <= self.index < len(context.scene.ndbe_jobs):
            job = context.scene.ndbe_jobs[self.index]
            if cancelled:
                job.status = "Cancelled"
                self.report({"WARNING"}, "Export cancelled.")
            elif return_code == 0:
                job.status = "Finished"
                self.report({"INFO"}, "FBX export finished.")
            else:
                message = self._last_error or "Headless export failed."
                job.status = "Failed: " + message
                self.report({"ERROR"}, message)

        shutil.rmtree(self._temp_dir, ignore_errors=True)
        return {"CANCELLED"} if cancelled or return_code != 0 else {"FINISHED"}


classes = (
    NDBE_MeshItem,
    NDBE_MergeGroup,
    NDBE_ExportJob,
    NDBE_UL_MeshList,
    NDBE_PT_Panel,
    NDBE_OT_AddJob,
    NDBE_OT_DeleteJob,
    NDBE_OT_SetExportMeshes,
    NDBE_OT_AddMergeGroup,
    NDBE_OT_DeleteMergeGroup,
    NDBE_OT_SetMergeGroupMeshes,
    NDBE_OT_ClearMergeGroupMeshes,
    NDBE_OT_StartExport,
)


def register():
    """Blenderへアドオンの型とシーンプロパティを登録する。"""
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.ndbe_jobs = bpy.props.CollectionProperty(type=NDBE_ExportJob)
    bpy.types.Scene.ndbe_jobs_index = bpy.props.IntProperty(name="Job Index", default=0)
    bpy.types.Scene.ndbe_is_exporting = bpy.props.BoolProperty(name="Is Exporting", default=False)


def unregister():
    """登録した型とシーンプロパティを解除する。"""
    del bpy.types.Scene.ndbe_is_exporting
    del bpy.types.Scene.ndbe_jobs_index
    del bpy.types.Scene.ndbe_jobs
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
