import eslint from "@eslint/js";
import globals from "globals";
import reactHooks from "eslint-plugin-react-hooks";
import typescript from "typescript-eslint";

export default typescript.config(
  { ignores: ["dist/**", "src/routeTree.gen.ts"] },
  eslint.configs.recommended,
  ...typescript.configs.recommended,
  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
    },
    plugins: {
      "react-hooks": reactHooks,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      "no-empty": "off",
    },
  },
);
