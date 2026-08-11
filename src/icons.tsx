import type { ReactNode } from "react";

function Icon({ children }: { children: ReactNode }) {
  return <svg style={{ display: "block" }} width="20" height="20" viewBox="0 0 24 24"
    fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    {children}
  </svg>;
}

export const tabIcons = {
  Monitor: <Icon><path d="M3 12h4l2-7 4 14 2-7h6" /></Icon>,
  Performance: <Icon><path d="M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z" /></Icon>,
  Fan: <Icon><circle cx="12" cy="12" r="2" /><path d="M12 10c-1.5-4.5.5-7 3-7 2 0 3 2 2 4.5S14 11 12 10M14 12c4.5-1.5 7 .5 7 3 0 2-2 3-4.5 2S13 14 14 12M12 14c1.5 4.5-.5 7-3 7-2 0-3-2-2-4.5S10 13 12 14M10 12c-4.5 1.5-7-.5-7-3 0-2 2-3 4.5-2S11 10 10 12" /></Icon>,
  Presets: <Icon><path d="M4 6h16M4 12h16M4 18h16" /><circle cx="8" cy="6" r="2" fill="#0d141c" /><circle cx="16" cy="12" r="2" fill="#0d141c" /><circle cx="10" cy="18" r="2" fill="#0d141c" /></Icon>,
  Logs: <Icon><path d="M6 2h9l4 4v16H6z" /><path d="M14 2v5h5M9 12h6M9 16h6" /></Icon>,
};
