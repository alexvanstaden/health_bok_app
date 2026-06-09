/** @type {import('next').NextConfig} */
// Standalone output keeps the production image small (only the traced runtime is
// copied — see web/Dockerfile).
const nextConfig = {
  output: "standalone",
  reactStrictMode: true,
};

module.exports = nextConfig;
