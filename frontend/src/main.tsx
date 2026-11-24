import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./styles.css";
import "./picker/font/noto-emoji.css";
import "./picker/picker.css";
import { applyEmojiStyle } from "./picker/preferences";
import { initLanguage } from "./i18n";
// The page in the user's language (backend/language.txt); English until loaded.
void initLanguage();
applyEmojiStyle();
ReactDOM.createRoot(document.getElementById("root")!).render(<React.StrictMode><App /></React.StrictMode>);
