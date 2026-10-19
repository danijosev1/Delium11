import { useEffect, useState } from "react";

const read = () => {
  const raw = window.location.hash.replace(/^#/, "");
  return raw || "/";
};

/** HashRouter equivalent — BrowserRouter does not work inside the preview iframe. */
export function useHashRoute(): string {
  const [path, setPath] = useState<string>(read);
  useEffect(() => {
    const onChange = () => setPath(read());
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return path;
}

export function navigate(to: string) {
  if (window.location.hash === `#${to}`) return;
  window.location.hash = to;
}
