import ast
import ctypes
import operator
import os
import re
from collections import Counter
from functools import cmp_to_key
from pathlib import Path

MEDIA_EXTENSIONS = {
    "image": frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}),
    "video": frozenset({".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}),
    "audio": frozenset({".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".opus"}),
}
DIRECTORY_FIELDS = {
    "directory", "directory_path", "dir_path", "folder", "folder_path", "input_dir",
    "input_directory", "input_folder", "image_directory", "image_folder", "video_folder",
    "source_dir", "source_directory", "source_folder",
}
SAVE_DIRECTORY_FIELDS = DIRECTORY_FIELDS | {
    "output_dir", "output_directory", "output_folder", "save_dir", "save_directory",
    "save_folder", "destination_dir", "dir_c",
}
PREFIX_FIELDS = ("filename_prefix", "file_prefix", "output_prefix", "prefix", "filename")
READ_WORDS = ("load", "read", "import", "source", "读取", "加载")
SAVE_WORDS = ("save", "export", "videocombine", "video combine", "保存", "写入")
SELECTORS = ("anythingindexswitch", "indexswitch", "index switch")
PATH_FIELDS = ("source_path", "path", "filepath", "file_path", "file", "filename")
MATH_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul}


def _natural_key(name):
    return [int(part) if part.isascii() and part.isdigit() else part.casefold()
            for part in re.split(r"([0-9]+)", name)]


def sort_names(values, name=str):
    if os.name == "nt":
        compare = ctypes.windll.shlwapi.StrCmpLogicalW
        compare.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p)
        compare.restype = ctypes.c_int

        def compare_values(left, right):
            a, b = name(left), name(right)
            return compare(a, b) or ((a > b) - (a < b))

        return sorted(values, key=cmp_to_key(compare_values))
    return sorted(values, key=lambda value: (_natural_key(name(value)), name(value)))


def path_value(value):
    return str(value or "").strip().strip('"')


def local_path(value, label):
    value = path_value(value)
    if not value:
        raise ValueError(f"请填写{label}")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{label}需要使用绝对路径: {path}")
    return path.resolve()


def link(value):
    if (isinstance(value, list) and len(value) == 2 and isinstance(value[0], str)
            and type(value[1]) is int):
        return value[0], value[1]
    return None


def checked_workflow(data):
    if isinstance(data, dict) and isinstance(data.get("nodes"), list):
        raise ValueError("该文件是 ComfyUI 画布格式。请在 ComfyUI 导出 API 格式 JSON")
    if isinstance(data, dict) and isinstance(data.get("prompt"), dict) and "class_type" not in data["prompt"]:
        data = data["prompt"]
    if not isinstance(data, dict) or not data:
        raise ValueError("请选择有效的 ComfyUI API 工作流 JSON")
    for node_id, node in data.items():
        if (not isinstance(node, dict) or not isinstance(node.get("class_type"), str)
                or not isinstance(node.get("inputs"), dict)):
            raise ValueError(f"节点 #{node_id} 缺少 class_type 或 inputs，请导出 API 格式")
    for node_id, node in data.items():
        for field, value in node["inputs"].items():
            connection = link(value)
            if connection and (connection[0] not in data or connection[1] < 0):
                raise ValueError(f"节点 #{node_id}.{field} 引用了无效连接 #{connection[0]}:{connection[1]}")
    return data


def node_title(node):
    meta = node.get("_meta")
    return str(meta.get("title") or "") if isinstance(meta, dict) else ""


def input_specs(info):
    specs = {}
    sections = info.get("input") or {}
    for section in ("required", "optional"):
        for field, value in (sections.get(section) or {}).items():
            if isinstance(value, (list, tuple)) and value:
                specs[field] = (value[0], value[1] if len(value) > 1 and isinstance(value[1], dict) else {})
    return specs


def scalar_math(expression, values):
    # API graphs sometimes supply a switch index through ComfyMathExpression.
    # Evaluate only a small constant arithmetic grammar, with bounded integers.
    if not isinstance(expression, str) or len(expression) > 100:
        return None

    def visit(node):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            result = node.value
        elif isinstance(node, ast.Name):
            result = values.get(node.id)
        elif isinstance(node, ast.BinOp) and type(node.op) in MATH_OPS:
            result = MATH_OPS[type(node.op)](visit(node.left), visit(node.right))
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            result = -visit(node.operand)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
            result = {"max": max, "min": min}[node.func.id](visit(arg) for arg in node.args)
        else:
            raise ValueError
        if type(result) not in (int, float) or not -1_000_000 <= result <= 1_000_000:
            raise ValueError
        return result

    try:
        return visit(ast.parse(expression, mode="eval").body)
    except (ValueError, TypeError, SyntaxError, KeyError, ArithmeticError, RecursionError):
        return None


class WorkflowGraph:
    def __init__(self, workflow, metadata=None):
        self.nodes = checked_workflow(workflow)
        self.metadata = metadata or {}
        self.specs = {node_id: input_specs(self.info(node)) for node_id, node in self.nodes.items()}

    def info(self, node):
        return self.metadata.get(node["class_type"]) or {}

    def constant(self, value, seen=()):
        connection = link(value)
        if not connection:
            return value
        node_id, slot = connection
        if node_id in seen or len(seen) >= 24:
            return None
        node = self.nodes[node_id]
        values = node["inputs"]
        seen = (*seen, node_id)
        if node["class_type"] == "ComfyMathExpression" and slot in (0, 1):
            variables = {name.split(".", 1)[1]: self.constant(item, seen)
                         for name, item in values.items() if name.startswith("values.")}
            result = scalar_math(values.get("expression"), variables)
            return int(result) if result is not None and slot == 1 else result
        if slot == 0 and len(values) == 1:
            field, item = next(iter(values.items()))
            if field in {"value", "selected_index_value", "text", "boolean"}:
                return self.constant(item, seen)
        return None

    def binding(self, node_id, field):
        path = []
        current = (node_id, field)
        while current not in path and len(path) < 24:
            path.append(current)
            node = self.nodes[current[0]]
            value = node["inputs"].get(current[1])
            if isinstance(value, str):
                return {"id": current[0], "field": current[1], "value": value,
                        "trace": [hop[0] for hop in path]}
            connection = link(value)
            if not connection:
                return None
            upstream = self.nodes[connection[0]]
            kind = upstream["class_type"].casefold()
            fields = upstream["inputs"]
            candidates = [key for key, item in fields.items()
                          if isinstance(item, str) or link(item)]
            output_types = self.info(upstream).get("output") or []
            typed_string = connection[1] < len(output_types) and output_types[connection[1]] == "STRING"
            pass_through = any(token in kind for token in ("primitive", "text", "string", "reroute", "passthrough"))
            if len(candidates) != 1 or not (pass_through or (typed_string and len(fields) == 1)):
                return None
            current = (connection[0], candidates[0])
        return None

    def upstream(self, roots):
        reachable, uncertain = set(), set()
        stack = [(root, False) for root in roots]
        while stack:
            node_id, maybe = stack.pop()
            if node_id in reachable or (maybe and node_id in uncertain):
                continue
            (uncertain if maybe else reachable).add(node_id)
            node = self.nodes[node_id]
            fields = node["inputs"]
            kind = node["class_type"].casefold()
            switched = any(kind_name in kind for kind_name in SELECTORS)
            picked = self.constant(fields.get("index")) if switched else None
            for key, value in fields.items():
                connection = link(value)
                if not connection:
                    continue
                option = re.fullmatch(r"(?:value|input|any)[_ ]?(\d+)", key)
                if switched and option and type(picked) is int and int(option[1]) != picked:
                    continue
                stack.append((connection[0], maybe or (switched and bool(option) and picked is None)))
        return reachable, uncertain - reachable

    def folder_scope(self, node):
        values = node["inputs"]
        for field in ("current_folder_only", "only_current_folder"):
            if field in values:
                result = self.constant(values[field])
                return result if type(result) is bool else None
        for field in ("recursive", "include_subfolders", "scan_subfolders"):
            if field in values:
                result = self.constant(values[field])
                return not result if type(result) is bool else None
        return False if node["class_type"] == "RuiImagePathReader" else None

    def inputs(self, reachable, uncertain):
        candidates = {}
        for node_id, node in self.nodes.items():
            kind = node["class_type"].casefold()
            title = node_title(node)
            label = f"{kind} {title.casefold()}"
            reads = any(word in label for word in READ_WORDS)
            values = node["inputs"]
            info = self.info(node)
            types = {str(value).upper() for value in (info.get("output") or [])}
            inferred_role = next((role for role in MEDIA_EXTENSIONS
                                  if role in label or role.upper() in types), "file")
            has_path_link = any(link(value) for field, value in values.items()
                                if field.casefold() in set(PATH_FIELDS) | {"image_path", "video_path", "audio_path"})
            for field, value in values.items():
                key = field.casefold()
                dtype, options = self.specs[node_id].get(field, (None, {}))
                upload_role = next((role for role in MEDIA_EXTENSIONS if options.get(f"{role}_upload")), None)
                directory = (key in DIRECTORY_FIELDS and reads) or dtype == "DIRECTORY" or any(
                    options.get(flag) for flag in ("directory_upload", "folder_upload", "image_directory_upload")
                )
                directory |= key in PATH_FIELDS and reads and any(
                    word in kind for word in ("pathreader", "folder", "directory"))
                if not directory and not upload_role and not reads:
                    continue
                if directory:
                    role, access, score = inferred_role, "directory", 88
                elif upload_role or (key in MEDIA_EXTENSIONS and reads):
                    role, access, score = upload_role or key, "upload", 96
                    # Path-only readers may name a path field after the media.
                    if not upload_role and isinstance(dtype, str) and dtype == "STRING" and "path" in kind:
                        access = "absolute"
                elif key in PATH_FIELDS or key in {"image_path", "video_path", "audio_path"}:
                    role, access, score = inferred_role, "absolute", 70
                else:
                    continue
                binding = self.binding(node_id, field)
                if not binding:
                    continue
                # The value computed by a workflow loop is not an external media input.
                if access != "directory" and link(value) and binding["id"] == node_id:
                    continue
                active = node_id in reachable or not reachable
                fallback = has_path_link and access == "upload" and not link(value)
                if fallback or node_id in uncertain or not active:
                    score = min(score, 55)
                scope = self.folder_scope(node) if directory else None
                details = "目录路径" if directory else ("ComfyUI 文件入口" if access == "upload" else "文件路径")
                item = {
                    **binding, "reader_id": node_id, "class_type": node["class_type"],
                    "title": title, "role": role, "access": access, "scope": scope,
                    "source_mode": "directory" if directory else "folder",
                    "enabled": score >= 65, "confidence": score, "reason": details,
                }
                target = (item["id"], item["field"])
                if target not in candidates or score > candidates[target]["confidence"]:
                    candidates[target] = item
        return list(candidates.values())

    def outputs(self):
        outputs = []
        for node_id, node in self.nodes.items():
            kind = node["class_type"].casefold()
            title = node_title(node)
            label = f"{kind} {title.casefold()}"
            values = node["inputs"]
            declared = bool(self.info(node).get("output_node"))
            directory = next((key for key in values if key.casefold() in SAVE_DIRECTORY_FIELDS), None)
            prefix = next((key for key in PREFIX_FIELDS if key in values), None)
            save_flag = self.constant(values.get("save_output"))
            probable = any(word in kind for word in SAVE_WORDS) or (
                declared and (bool(directory or prefix) or any(word in label for word in SAVE_WORDS))
            )
            preview = "preview" in kind or save_flag is False
            if not probable and not (declared or preview):
                continue
            binding = self.binding(node_id, directory) if directory and probable else None
            prefix = prefix if prefix and isinstance(values[prefix], str) else None
            outputs.append({
                "id": node_id, "class_type": node["class_type"], "title": title,
                "field": prefix, "binding": binding, "enabled": probable and not preview and (not directory or bool(binding)),
                "mode": "direct" if directory and probable else "history", "preview": preview,
                "reason": ("预览或未启用保存" if preview else "工作流指定保存目录" if directory and probable
                           else "ComfyUI 历史成品" if probable else "请确认此节点是否保存成品"),
            })
        return outputs

    def analyze(self):
        outputs = self.outputs()
        roots = [item["id"] for item in outputs if item["enabled"]]
        reachable, uncertain = self.upstream(roots)
        inputs = self.inputs(reachable, uncertain)
        for item in inputs:
            item["confidence_label"] = "高" if item["confidence"] >= 85 else "中" if item["confidence"] >= 65 else "待确认"
        directory_inputs = [item for item in inputs if item["enabled"] and item["access"] == "directory"]
        if len(directory_inputs) == 2 and all(item["scope"] is True for item in directory_inputs):
            for item in directory_inputs:
                item["source_mode"] = "groups"
        editable = []
        for node_id, node in self.nodes.items():
            fields = [field for field, value in node["inputs"].items() if isinstance(value, str)]
            editable.append({"id": node_id, "class_type": node["class_type"],
                             "title": node_title(node), "fields": fields})
        unknown = sorted({node["class_type"] for node in self.nodes.values()
                          if node["class_type"] not in self.metadata}) if self.metadata else []
        bad_slots = []
        for node_id, node in self.nodes.items():
            for value in node["inputs"].values():
                connection = link(value)
                if not connection:
                    continue
                output_types = self.info(self.nodes[connection[0]]).get("output")
                if isinstance(output_types, list) and connection[1] >= len(output_types):
                    bad_slots.append(f"#{node_id} 引用了不存在的槽位 #{connection[0]}:{connection[1]}")
        return {"inputs": inputs, "outputs": outputs, "nodes": editable,
                "missing_types": unknown, "connection_warnings": bad_slots}


def _field(workflow, node_id, field):
    if not isinstance(workflow.get(node_id, {}).get("inputs", {}).get(field), str):
        raise ValueError(f"#{node_id}.{field} 不是可替换的文本字段")
    return node_id, field


def normalize_bindings(workflow, analysis, body):
    known_inputs = {(item["id"], item["field"]): item for item in analysis["inputs"]}
    known_outputs = {item["id"]: item for item in analysis["outputs"]}
    inputs, outputs = [], []
    occupied = set()
    for item in body.get("inputs", []):
        if not item.get("enabled"):
            continue
        node_id, field = _field(workflow, str(item.get("id")), item.get("field"))
        if (node_id, field) in occupied:
            raise ValueError(f"#{node_id}.{field} 已绑定")
        occupied.add((node_id, field))
        existing = known_inputs.get((node_id, field), {})
        mode = item.get("source_mode")
        role = item.get("role", "file")
        access = item.get("access") or existing.get("access", "upload")
        if mode not in {"single", "folder", "directory", "groups"} or role not in {*MEDIA_EXTENSIONS, "file"}:
            raise ValueError(f"#{node_id}.{field} 的素材方式无效")
        if access not in {"upload", "absolute", "directory"} or ((access == "directory") != (mode in {"directory", "groups"})):
            raise ValueError(f"#{node_id}.{field} 的路径类型与素材方式不匹配")
        if existing.get("scope") is False and mode == "groups":
            raise ValueError(f"#{node_id}.{field} 的读取节点未启用「仅当前目录」")
        if mode == "groups" and existing.get("scope") is not True and not item.get("confirm_scope"):
            raise ValueError(f"请确认 #{node_id}.{field} 只读取传入子目录这一组素材")
        inputs.append({
            "id": node_id, "field": field, "source_mode": mode, "access": access, "role": role,
            "reader_id": existing.get("reader_id", node_id),
            "source_path": str(local_path(item.get("source_path"), f" #{node_id}.{field} 素材路径")),
        })
    selected = set()
    for item in body.get("outputs", []):
        if not item.get("enabled"):
            continue
        node_id = str(item.get("id"))
        if node_id not in workflow or node_id in selected:
            raise ValueError(f"输出节点 #{node_id} 无效或重复")
        selected.add(node_id)
        existing = known_outputs.get(node_id, {})
        mode = item.get("mode") or existing.get("mode", "history")
        if mode not in {"history", "direct"}:
            raise ValueError(f"输出节点 #{node_id} 的保存方式无效")
        if existing.get("preview"):
            raise ValueError(f"#{node_id} 是预览或未启用保存，请在工作流中启用正式保存后再导入")
        output = {"id": node_id, "mode": mode, "field": existing.get("field")}
        if mode == "direct":
            binding = item.get("binding") or existing.get("binding") or {}
            dest_id, dest_field = _field(workflow, str(binding.get("id")), binding.get("field"))
            if (dest_id, dest_field) in occupied:
                raise ValueError(f"输出路径 #{dest_id}.{dest_field} 与素材输入重复")
            output["binding"] = {"id": dest_id, "field": dest_field}
        outputs.append(output)
    if not outputs:
        raise ValueError("请启用至少一个成品输出节点")
    return inputs, outputs


def _files(directory, role):
    if not directory.is_dir():
        raise ValueError(f"找不到目录: {directory}")
    extensions = MEDIA_EXTENSIONS.get(role)
    return sort_names(
        (path for path in directory.iterdir()
         if path.is_file() and (extensions is None or path.suffix.lower() in extensions)),
        lambda path: path.name,
    )


def _pair_directories(left, right):
    right_names = {os.path.normcase(path.name): path for path in right}
    matches = {p: right_names[os.path.normcase(p.name)] for p in left if os.path.normcase(p.name) in right_names}
    reserved = set(matches.values())
    remaining_left = [p for p in left if p not in matches]
    remaining_right = [p for p in right if p not in reserved]
    fallback = dict(zip(remaining_left, remaining_right))
    pairs = [(path, matches[path], "同名") if path in matches else (path, fallback[path], "自然顺序")
             for path in left if path in matches or path in fallback]
    used_left = {pair[0] for pair in pairs}
    used_right = {pair[1] for pair in pairs}
    return pairs, [str(p) for p in left if p not in used_left] + [str(p) for p in right if p not in used_right]


def _safe_name(text):
    return re.sub(r"[^\w.-]+", "_", text).strip(" ._")


def base_name(paths, index, groups=False):
    if groups:
        label = _safe_name(paths[0].name)
    else:
        names = list(dict.fromkeys(_safe_name(path.stem) for path in paths))
        label = "_".join(filter(None, names[:2]))
    return label[:80].rstrip(" .") or f"batch_{index + 1:04d}"


def plan_batches(inputs, workflow=None, outputs=()):
    fixed, streams, groups = {}, {}, {}
    for index, item in enumerate(inputs):
        path = Path(item["source_path"])
        mode = item["source_mode"]
        if mode == "single":
            if not path.is_file():
                raise ValueError(f"找不到单文件: {path}")
            fixed[index] = path
        elif mode == "directory":
            if not _files(path, item["role"]):
                raise ValueError(f"该目录没有可用素材: {path}")
            fixed[index] = path
        elif mode == "groups":
            if not path.is_dir():
                raise ValueError(f"找不到总目录: {path}")
            groups[index] = sort_names((p for p in path.iterdir() if p.is_dir()), lambda p: p.name)
            if not groups[index]:
                raise ValueError(f"总目录没有一级子文件夹: {path}")
        else:
            streams[index] = _files(path, item["role"])
            if not streams[index]:
                raise ValueError(f"该目录没有可用素材: {path}")

    items, unmatched = [], []
    if groups:
        if len(groups) != 2 or streams:
            raise ValueError("双目录批量需要且只需要两个总目录入口。其他入口可设为固定素材")
        a, b = groups
        pairs, unmatched = _pair_directories(groups[a], groups[b])
        for index, (left, right, match) in enumerate(pairs):
            row = {**fixed, a: left, b: right}
            sizes = {key: len(_files(path, inputs[key]["role"])) for key, path in ((a, left), (b, right))}
            if not all(sizes.values()):
                invalid = left if not sizes[a] else right
                raise ValueError(f"配对目录没有符合类型的素材: {invalid}")
            expected = expected_direct_count(workflow, outputs, inputs, sizes)
            items.append({
                "entries": [{**inputs[key], "source": str(row[key])} for key in range(len(inputs))],
                "label": f"{left.name} / {right.name}", "match": match,
                "base_name": base_name([left, right], index, groups=True), "expected_direct": expected,
            })
    else:
        total = min(map(len, streams.values())) if streams else 1
        for paths in streams.values():
            unmatched += [str(path) for path in paths[total:]]
        for index in range(total):
            row = {**fixed, **{key: paths[index] for key, paths in streams.items()}}
            names = [row[key] for key in row]
            items.append({
                "entries": [{**inputs[key], "source": str(row[key])} for key in range(len(inputs))],
                "label": " / ".join(path.name for path in names) or "单次执行",
                "match": "文件顺序" if streams else "固定素材", "base_name": base_name(names, index),
            })
    if not items:
        raise ValueError("没有可运行的批次")
    return items, unmatched


def expected_direct_count(workflow, outputs, inputs, sizes):
    # A known plugin contract can supply a count. Other workflows are checked
    # against their actual history or the isolated files written in this attempt.
    if not workflow or len(outputs) != 1 or outputs[0]["mode"] != "direct":
        return None
    saver = workflow[outputs[0]["id"]]
    if saver["class_type"] != "RuiImageSaver":
        return None
    image_link = link(saver["inputs"].get("image_path_A"))
    if not image_link or workflow[image_link[0]]["class_type"] != "RuiImagePairLoopStart":
        return None
    loop = workflow[image_link[0]]["inputs"]
    plan_link = link(loop.get("pair_plan"))
    if not plan_link or workflow[plan_link[0]]["class_type"] != "RuiImagePairPlan":
        return None
    plan = workflow[plan_link[0]]["inputs"]
    reader_links = [link(plan.get(f"groups_{name}")) for name in ("a", "b")]
    if not all(reader_links) or Counter(connection[0] for connection in reader_links) != Counter(
        inputs[key]["reader_id"] for key in sizes
    ):
        return None
    graph = WorkflowGraph(workflow)
    counts = []
    for key, count in sizes.items():
        node = workflow[inputs[key]["reader_id"]]
        if node["class_type"] != "RuiImagePathReader":
            return None
        skip = graph.constant(node["inputs"].get("skip_last_n", 0))
        if type(skip) is not int:
            return None
        counts.append(count - skip)
    mode = plan.get("match_mode")
    if mode == "flat_by_folder":
        raise ValueError("该工作流仍使用平铺图按组分配，请改为支持两个当前目录的组内匹配模式")
    if mode not in {"cycle_shorter", "strict_one_to_one"}:
        return None
    if min(counts) < 1 or (mode == "strict_one_to_one" and len(set(counts)) != 1):
        raise ValueError("本组的图片数量不符合工作流内的配对设置，请检查图片和跳过末尾张数")
    start = loop.get("start_index", 0)
    if type(start) is int and not 0 <= start < max(counts):
        raise ValueError("工作流的组内循环起点超出当前组的配对数量")
    return max(counts) - start if type(start) is int else None
