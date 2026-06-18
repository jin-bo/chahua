"use strict";

// 共享文件图标 —— composer 待发 pill 与气泡下载 pill 共用。
//
// 「页形角标」(folder dog-ear) + 扩展名文字（textContent，无注入），按类别上色。
// 镜像 ../guanlan/guanlan/web/static 的 fileTypeBadge：比单一 📄 emoji 更能一眼辨
// 类型（PDF 红 / 代码紫 / 表格绿 …），且与系统 emoji 字形无关、跨平台稳定。
//
// 配色与扩展名映射见 styles/composer.css 的 `.file-ico[data-cat]` 段（两处 pill 共享
// 同一份 CSS，只在尺寸上各自缩放）。

// 扩展名 → 类别。未命中走 "file"（灰）。键名全小写，调用方负责 toLowerCase。
// 用 null 原型表 —— 否则 `FILE_CAT["constructor"]` 等会命中 Object.prototype 上的
// 同名属性（返回函数，truthy），让 `x.constructor` 这类病态扩展名查到假类别。
const FILE_CAT = Object.assign(Object.create(null), {
  pdf: "pdf",
  doc: "doc", docx: "doc", rtf: "doc", odt: "doc",
  xls: "xls", xlsx: "xls", csv: "xls", tsv: "xls", ods: "xls",
  ppt: "ppt", pptx: "ppt", key: "ppt", odp: "ppt",
  zip: "zip", tar: "zip", gz: "zip", tgz: "zip", "7z": "zip", rar: "zip", bz2: "zip", xz: "zip",
  md: "md", markdown: "md",
  txt: "txt", text: "txt", log: "txt", rst: "txt", org: "txt",
  json: "code", yaml: "code", yml: "code", toml: "code", xml: "code", html: "code", htm: "code",
  css: "code", scss: "code", js: "code", mjs: "code", ts: "code", tsx: "code", jsx: "code",
  py: "code", sh: "code", bash: "code", c: "code", h: "code", cpp: "code", go: "code",
  rs: "code", java: "code", rb: "code", php: "code", lua: "code", sql: "code",
  png: "img", jpg: "img", jpeg: "img", gif: "img", webp: "img", svg: "img", bmp: "img",
  tiff: "img", tif: "img", ico: "img", heic: "img",
  mp3: "audio", wav: "audio", flac: "audio", m4a: "audio", ogg: "audio", aac: "audio",
  mp4: "video", mov: "video", mkv: "video", webm: "video", avi: "video", m4v: "video",
});

// 文件名 → {ext, cat, label}。取 basename（name 可能含子目录前缀如 tasks/<id>/...）；
// "." 在首位（dotfile，如 .gitignore）当无扩展名走 FILE 兜底，不把整名当类型显示。
// label 截到 4 字符防角标溢出（如 "MARKDOWN" → "MARK"）。
export function fileKindInfo(name) {
  const s = typeof name === "string" ? name : "";
  const base = s.slice(s.lastIndexOf("/") + 1);
  const dot = base.lastIndexOf(".");
  const ext = dot > 0 ? base.slice(dot + 1).toLowerCase() : "";
  return {
    ext,
    cat: FILE_CAT[ext] || "file",
    label: ext ? ext.toUpperCase().slice(0, 4) : "FILE",
  };
}

// 文件类型角标 DOM：<span class="file-ico" data-cat="..">EXT</span>。
// 折角与上色全交给 CSS；这里只填类别与文字。
export function fileTypeBadge(name) {
  const { cat, label } = fileKindInfo(name);
  const span = document.createElement("span");
  span.className = "file-ico";
  span.dataset.cat = cat;
  span.textContent = label;
  return span;
}
