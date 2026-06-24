# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **Mermaid diagrams in `docs/pdf/state-diagram.pdf`.** The diagrams previously
  rendered as broken image placeholders: relative image paths did not resolve in
  the PDF renderer, and the hi-res images overflowed the page and spilled across
  extra pages. Now the Mermaid blocks are rendered to hi-res PNGs and embedded as
  width-constrained, base64 data-URI images, so both the lifecycle and
  compensation sub-machine diagrams display correctly and crisply.
