/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // The user's home directory also contains a package-lock.json. Without an explicit
  // root, Turbopack walks upward, warns about that unrelated file, and can treat the
  // wrong directory as the build boundary. `serve.sh` and documented commands both run
  // from `web/`, so the current directory is the complete frontend workspace.
  turbopack: { root: process.cwd() },
};

export default nextConfig;
