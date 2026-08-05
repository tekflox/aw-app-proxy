# Proxy

Proxy provides authenticated browsing support for Browser in an AW Workspace. It gives the workspace a private browser proxy and a cookie-sync service so the workspace browser can access sites the user is already signed into.

## What It Does

- Runs a private HTTP CONNECT proxy for the workspace browser.
- Receives selected cookies from supported browser extensions.
- Stores chosen cookies securely so browser access survives restarts.
- Injects or clears cookies in the workspace browser when needed.
- Provides settings and logs for proxy behavior.

## Why Use It

Use this app with Browser when web work requires authenticated sessions. It is useful for testing private pages, using web dashboards, validating user flows behind login, and letting agents operate pages that need the user's existing access.

## How To Use It

Install Proxy before Browser. Configure its settings if the defaults do not match the workspace network. Use the supported cookie-sync extension to send browser cookies into the workspace, then open Browser and visit the authenticated site.

## What It Delivers

The app gives AW Workspace a controlled way to support authenticated browsing. It keeps Browser useful for real logged-in workflows while keeping the proxy private to the workspace environment.
