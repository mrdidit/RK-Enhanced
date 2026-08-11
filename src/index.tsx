import { definePlugin } from "@decky/api";
import { FaSlidersH } from "react-icons/fa";
import { Content } from "./Content";

export default definePlugin(() => ({
  name: "RK-Enhanced",
  title: <div>RK-Enhanced</div>,
  content: <Content />,
  icon: <FaSlidersH />,
  alwaysRender: true,
}));
