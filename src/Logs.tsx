import { Field, PanelSection, PanelSectionRow } from "@decky/ui";
import { useCallback, useEffect, useState } from "react";
import { getLog } from "./backend";

export function Logs() {
  const [content, setContent] = useState("Loading log…");
  const [error, setError] = useState("");
  const refresh = useCallback(async () => {
    try {
      setContent((await getLog()).split("\n").reverse().join("\n"));
      setError("");
    } catch (reason) {
      setError(String(reason));
    }
  }, []);
  useEffect(() => {
    void refresh();
    const timer = window.setInterval(refresh, 2000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  return <PanelSection title="RK-Enhanced Log">
    {error && <PanelSectionRow><Field label={error} /></PanelSectionRow>}
    <PanelSectionRow>
      <pre style={{
        boxSizing: "border-box", width: "100%", margin: 0, padding: 10,
        minHeight: 480,
        borderRadius: 6, background: "rgba(0,0,0,.35)", color: "#d7e0e8",
        fontFamily: "monospace", fontSize: 14, lineHeight: 1.45,
        whiteSpace: "pre-wrap", overflowWrap: "anywhere", userSelect: "text", overflowY: "auto",
      }}>{content || "Log is empty."}</pre>
    </PanelSectionRow>
  </PanelSection>;
}
