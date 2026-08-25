import { gamepadDialogClasses, quickAccessMenuClasses } from "@decky/ui";

export const styles = `
  .rke-tabs { height: 95%; width: 306px; position: fixed; margin-top: -12px; margin-left: 0; overflow: hidden; contain: paint; isolation: isolate; clip-path: inset(0); }
  .rke-tabs > div:nth-of-type(2) > div:first-child::before { background: #0d141c; box-shadow: none; backdrop-filter: none; }
  .rke-tabs [role="tablist"] { height: 58px !important; opacity: 0 !important; }
  .rke-tab-header { position: absolute; z-index: 20; top: 0; left: 6px; right: 6px; height: 58px; display: flex; align-items: center; justify-content: center; pointer-events: none; }
  .rke-tab-menu { display: flex; align-items: center; justify-content: center; gap: 11px; min-width: 0; }
  .rke-shoulder-glyph { width: 46px; height: 26px; flex: 0 0 auto; overflow: visible; }
  .rke-active-tab { min-width: 122px; max-width: 150px; box-sizing: border-box; padding: 9px 14px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; border: 2px solid rgba(255,255,255,.24); border-radius: 20px; background: #3c454e; color: #f4f6f7; text-align: center; text-transform: uppercase; font-size: 14px; line-height: 18px; font-weight: 700; letter-spacing: .04em; transition: border-color .12s ease, background .12s ease, box-shadow .12s ease; }
  .rke-active-tab-focused,
  .rke-tabs:has([role="tablist"]:focus-within) .rke-active-tab { border-color: rgba(255,255,255,.82); background: #555e68; box-shadow: 0 0 0 2px rgba(255,255,255,.24); }
  .rke-tabs [role="tabpanel"] { box-sizing: border-box; padding-left: 4px !important; padding-right: 6px !important; overflow-x: clip !important; overflow-y: auto !important; touch-action: pan-y; overscroll-behavior-y: contain; contain: paint; clip-path: inset(0); }
  .rke-content { box-sizing: border-box; width: 100%; padding: 0 2px 28px; overflow-x: clip; contain: paint; }
  .rke-save-apply { box-sizing: border-box; width: calc(100% - 12px); margin: 5px 0 7px 12px; }
  .rke-save-apply .${gamepadDialogClasses.Field} { opacity: .72; font-size: 11px; text-align: center; }
  .rke-fan-warning .${gamepadDialogClasses.Field},
  .rke-fan-warning .${gamepadDialogClasses.FieldDescription} { color: #ff5b5b !important; opacity: 1 !important; }
  .rke-boost-notice .${gamepadDialogClasses.Field},
  .rke-boost-notice .${gamepadDialogClasses.FieldDescription} { color: #69b9ff !important; opacity: 1 !important; }
  .rke-boost-warning .${gamepadDialogClasses.Field},
  .rke-boost-warning .${gamepadDialogClasses.FieldDescription} { color: #ff5b5b !important; opacity: 1 !important; }
  .rke-experimental-error .${gamepadDialogClasses.Field},
  .rke-experimental-error .${gamepadDialogClasses.FieldDescription} { color: #ff5b5b !important; opacity: 1 !important; }
  .rke-experimental-warning .${gamepadDialogClasses.Field},
  .rke-experimental-warning .${gamepadDialogClasses.FieldDescription} { color: #fed330 !important; opacity: 1 !important; }
  .rke-experimental-notice .${gamepadDialogClasses.Field},
  .rke-experimental-notice .${gamepadDialogClasses.FieldDescription} { color: #69b9ff !important; opacity: 1 !important; }
  .rke-rgb-error .${gamepadDialogClasses.Field},
  .rke-rgb-error .${gamepadDialogClasses.FieldDescription} { color: #ff5b5b !important; opacity: 1 !important; }
  .rke-rgb-notice .${gamepadDialogClasses.Field},
  .rke-rgb-notice .${gamepadDialogClasses.FieldDescription} { color: #69b9ff !important; opacity: 1 !important; }
  .rke-rgb-colour-value { display: flex; align-items: center; justify-content: flex-end; gap: 9px; width: 100%; color: rgba(255,255,255,.78); font-variant-numeric: tabular-nums; font-weight: 600; }
  .rke-rgb-swatch { display: block; width: 30px; height: 30px; flex: 0 0 30px; box-sizing: border-box; border: 1px solid rgba(255,255,255,.5); border-radius: 8px; box-shadow: inset 0 0 0 1px rgba(0,0,0,.22); }
  .rke-monitor-heading-row { display: flex; align-items: center; gap: 7px; box-sizing: border-box; width: 100%; padding: 7px 7px 4px; }
  .rke-monitor-heading-row::before,
  .rke-monitor-heading-row::after { content: ""; height: 1px; flex: 1 1 auto; background: rgba(255,255,255,.2); }
  .rke-monitor-heading { flex: 0 0 180px !important; width: 180px !important; min-width: 180px !important; box-sizing: border-box; padding: 5px 14px !important; border: 1px solid rgba(255,255,255,.28); border-radius: 12px; background: rgba(255,255,255,.06); color: #f2f4f5; text-align: center; font-size: 14px; line-height: 18px; font-weight: 600; transition: border-color .12s ease, background .12s ease; }
  .rke-monitor-heading-label { display: block; width: 100%; text-align: center; white-space: nowrap; }
  .rke-performance-heading-row { display: flex; align-items: center; gap: 7px; box-sizing: border-box; width: 100%; padding: 7px 7px 4px; }
  .rke-performance-heading-row::before,
  .rke-performance-heading-row::after { content: ""; height: 1px; flex: 1 1 auto; background: rgba(255,255,255,.2); }
  .rke-performance-heading { flex: 0 0 196px !important; width: 196px !important; min-width: 196px !important; box-sizing: border-box; padding: 5px 14px !important; border: 1px solid rgba(255,255,255,.28); border-radius: 12px; background: rgba(255,255,255,.06); color: #f2f4f5; text-align: center; font-size: 14px; line-height: 18px; font-weight: 600; transition: border-color .12s ease, background .12s ease; }
  .rke-performance-heading-label { display: block; width: 100%; text-align: center; white-space: nowrap; }
  .rke-performance-heading-label small { display: block; margin-top: 1px; opacity: .66; font-size: 11px; line-height: 14px; font-weight: 400; }
  .rke-frequency-label { display: flex; align-items: center; justify-content: space-between; gap: 8px; width: 100%; white-space: nowrap; }
  .rke-content .${quickAccessMenuClasses.PanelSectionRow} { margin-top: 0 !important; margin-bottom: 3px !important; padding-top: 0 !important; padding-bottom: 0 !important; }
  .rke-content .${gamepadDialogClasses.Field} { min-height: 0 !important; padding-top: 7px !important; padding-bottom: 7px !important; }
  .rke-content .${gamepadDialogClasses.WithStandardPadding},
  .rke-content .${gamepadDialogClasses.StandardPadding} { padding-top: 7px !important; padding-bottom: 7px !important; }
  .rke-content .${gamepadDialogClasses.StandardSpacing} { margin-top: 3px !important; margin-bottom: 3px !important; }
  .rke-tabs [role="tabpanel"] > div { min-width: 0; width: 100%; }
  .rke-tabs * { min-width: 0; }
`;
