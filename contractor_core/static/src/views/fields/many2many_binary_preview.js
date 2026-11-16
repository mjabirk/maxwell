/** @odoo-module */

import { patch } from "@web/core/utils/patch";
import { FileModel } from "@web/core/file_viewer/file_model";
import { useFileViewer } from "@web/core/file_viewer/file_viewer_hook";
import { Many2ManyBinaryField } from "@web/views/fields/many2many_binary/many2many_binary_field";

const EXT_TO_MIMETYPE = {
    pdf: "application/pdf",
    png: "image/png",
    jpg: "image/jpeg",
    jpeg: "image/jpeg",
    gif: "image/gif",
    webp: "image/webp",
    bmp: "image/bmp",
    svg: "image/svg+xml",
    tif: "image/tiff",
    tiff: "image/tiff",
    ico: "image/x-icon",
    txt: "text/plain",
    log: "text/plain",
    csv: "text/plain",
    json: "application/json",
    html: "text/html",
    htm: "text/html",
    css: "text/css",
    js: "application/javascript",
    mp4: "video/mp4",
    webm: "video/webm",
    mkv: "video/x-matroska",
    mp3: "audio/mpeg",
    dwg: "application/acad",
};

patch(Many2ManyBinaryField.prototype, {
    setup() {
        super.setup(...arguments);
        this.fileViewer = useFileViewer();
    },

    _toFileModel(file) {
        const fm = new FileModel();
        fm.id = file.id;
        fm.name = file.name || `attachment_${file.id}`;
        const rawMime = (file.mimetype || "").toLowerCase();
        const ext = (this.getExtension(file) || "").toLowerCase();
        if (rawMime === "application/x-pdf") {
            fm.mimetype = "application/pdf";
        } else if ((!rawMime || rawMime === "application/octet-stream") && EXT_TO_MIMETYPE[ext]) {
            fm.mimetype = EXT_TO_MIMETYPE[ext];
        } else {
            fm.mimetype = file.mimetype || "";
        }
        return fm;
    },

    canPreview(file) {
        return this._toFileModel(file).isViewable;
    },

    getPreviewUrl(file) {
        return this._toFileModel(file).defaultSource;
    },

    onPreviewFile(ev, file) {
        const target = this._toFileModel(file);
        if (!target.isViewable) {
            return;
        }
        ev.preventDefault();
        ev.stopPropagation();
        const viewableFiles = this.files
            .map((f) => this._toFileModel(f))
            .filter((f) => f.isViewable);
        const startFile = viewableFiles.find((f) => f.id === target.id) || target;
        this.fileViewer.open(startFile, viewableFiles.length ? viewableFiles : [startFile]);
    },
});

