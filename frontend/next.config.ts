import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // local dev only: proxy /api to the backend (vercel.json routes it in prod)
  async rewrites() {
    if (process.env.NODE_ENV !== "development") return [];
    return [{ source: "/api/:path*", destination: "http://127.0.0.1:8000/api/:path*" }];
  },
};

export default nextConfig;
