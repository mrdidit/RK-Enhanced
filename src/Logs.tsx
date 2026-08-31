import { ConfirmModal, Focusable } from "@decky/ui";
import type { CSSProperties } from "react";
import { useCallback, useEffect, useRef, useState } from "react";
import { getLog } from "./backend";
import { formatLogContent, startCompletionPoll } from "./frontendLightweight";

const logViewportStyle: CSSProperties = {
  boxSizing: "border-box",
  width: "min(1100px, calc(100vw - 200px))",
  height: "min(600px, 62vh)",
  minWidth: 0,
  maxWidth: "100%",
  marginLeft: "auto",
  marginRight: "auto",
  padding: "12px 14px",
  overflowX: "hidden",
  overflowY: "auto",
  border: "1px solid rgba(255,255,255,.22)",
  borderRadius: 8,
  background: "rgba(0,0,0,.38)",
};

const logTextStyle: CSSProperties = {
  boxSizing: "border-box",
  width: "100%",
  minWidth: 0,
  maxWidth: "100%",
  margin: 0,
  color: "#d7e0e8",
  fontFamily: "monospace",
  fontSize: 14,
  lineHeight: 1.45,
  whiteSpace: "pre-wrap",
  overflowWrap: "anywhere",
  wordBreak: "break-word",
  userSelect: "text",
};

export function Logs({ closeModal }: { closeModal?: () => void }) {
  const [content, setContent] = useState("Loading log…");
  const [error, setError] = useState("");
  const contentRef = useRef(content);
  const errorRef = useRef(error);
  const refresh = useCallback(async () => {
    try {
      const next = formatLogContent(await getLog());
      if (contentRef.current !== next) {
        contentRef.current = next;
        setContent(next);
      }
      if (errorRef.current) {
        errorRef.current = "";
        setError("");
      }
    } catch (reason) {
      const next = String(reason);
      if (errorRef.current !== next) {
        errorRef.current = next;
        setError(next);
      }
    }
  }, []);
  useEffect(() => {
    return startCompletionPoll(refresh, 2000);
  }, [refresh]);

  return <ConfirmModal
    bAlertDialog
    bAllowFullSize
    className="rke-log-modal-root"
    modalClassName="rke-log-modal"
    strTitle="RK-Enhanced Logs"
    strOKButtonText="Close"
    closeModal={closeModal}
    onCancel={closeModal}
    onOK={closeModal}>
    <style>{`
      .rke-log-modal,
      .rke-log-modal-root {
        box-sizing: border-box !important;
        width: min(1220px, calc(100vw - 120px)) !important;
        max-width: calc(100vw - 120px) !important;
        margin-left: auto !important;
        margin-right: auto !important;
        align-self: center !important;
      }
    `}</style>
    {error && <div style={{
      boxSizing: "border-box", width: "100%", marginBottom: 8, padding: "8px 10px",
      borderRadius: 6, background: "rgba(252,92,101,.16)", color: "#ff7b83",
      overflowWrap: "anywhere",
    }}>{error}</div>}
    <Focusable tabIndex={0} style={logViewportStyle}>
      <pre style={logTextStyle}>{content || "Log is empty."}</pre>
    </Focusable>
  </ConfirmModal>;
}
