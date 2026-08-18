import { gamepadDialogClasses, quickAccessMenuClasses } from "@decky/ui";

export const styles = `
  .rke-tabs { height: 95%; width: 306px; position: fixed; margin-top: -12px; margin-left: 0; overflow: hidden; contain: paint; isolation: isolate; clip-path: inset(0); }
  .rke-tabs > div:nth-of-type(2) > div:first-child::before { background: #0d141c; box-shadow: none; backdrop-filter: none; }
  .rke-tabs [role="tablist"] { height: 52px !important; opacity: 0 !important; }
  .rke-tab-header { position: absolute; z-index: 20; top: 0; left: 6px; right: 6px; height: 52px; display: flex; align-items: center; justify-content: center; pointer-events: none; }
  .rke-tab-menu { display: flex; align-items: center; justify-content: center; gap: 7px; min-width: 0; }
  .rke-shoulder-glyph { width: 25px; height: 25px; flex: 0 0 auto; overflow: visible; }
  .rke-active-tab { min-width: 112px; max-width: 180px; box-sizing: border-box; padding: 8px 15px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; border-radius: 18px; background: #3c454e; color: #f4f6f7; text-align: center; text-transform: uppercase; font-size: 12px; line-height: 16px; font-weight: 700; letter-spacing: .04em; }
  .rke-tabs [role="tabpanel"] { box-sizing: border-box; padding-left: 4px !important; padding-right: 6px !important; overflow-x: clip !important; overflow-y: auto !important; touch-action: pan-y; overscroll-behavior-y: contain; contain: paint; clip-path: inset(0); }
  .rke-content { box-sizing: border-box; width: 100%; padding: 0 2px 28px; overflow-x: clip; contain: paint; }
  .rke-presets .rke-action-button { box-sizing: border-box !important; width: calc(100% - 12px) !important; margin-left: 12px !important; }
  .rke-save-apply { box-sizing: border-box; width: calc(100% - 12px); margin: 5px 0 7px 12px; }
  .rke-save-apply .${gamepadDialogClasses.Field} { opacity: .72; font-size: 11px; text-align: center; }
  .rke-fan-warning .${gamepadDialogClasses.Field},
  .rke-fan-warning .${gamepadDialogClasses.FieldDescription} { color: #ff5b5b !important; opacity: 1 !important; }
  .rke-section-heading { width: 100%; box-sizing: border-box; padding: 8px 0 10px; text-align: center; text-transform: uppercase; font-size: 16px; font-weight: 700; letter-spacing: .04em; }
  .rke-cluster-heading { width: 100%; box-sizing: border-box; padding: 12px 0 4px; text-align: center; font-size: 15px; font-weight: 600; }
  .rke-cluster-heading small { display: block; margin-top: 3px; opacity: .66; font-size: 11px; font-weight: 400; }
  .rke-content .${quickAccessMenuClasses.PanelSectionRow} { margin-top: 0 !important; margin-bottom: 3px !important; padding-top: 0 !important; padding-bottom: 0 !important; }
  .rke-content .${gamepadDialogClasses.Field} { min-height: 0 !important; padding-top: 7px !important; padding-bottom: 7px !important; }
  .rke-content .${gamepadDialogClasses.WithStandardPadding},
  .rke-content .${gamepadDialogClasses.StandardPadding} { padding-top: 7px !important; padding-bottom: 7px !important; }
  .rke-content .${gamepadDialogClasses.StandardSpacing} { margin-top: 3px !important; margin-bottom: 3px !important; }
  .rke-tabs [role="tabpanel"] > div { min-width: 0; width: 100%; }
  .rke-tabs * { min-width: 0; }
`;
