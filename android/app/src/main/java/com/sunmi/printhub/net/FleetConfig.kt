package com.sunmi.printhub.net

/**
 * Fleet "Hershey Highway" broadcast channel — a private, always-on listener so a central
 * server (hosted at [DOMAIN]) can push one message to every printer in the fleet at once.
 *
 * Auth is a shared static [CODE] baked into the app that bypasses the per-device access
 * password. This is deliberately a possession-based fleet secret ("has the app => on the
 * fleet"), suitable for a private printer network — it is NOT strong authentication, since a
 * value compiled into an APK is extractable. Keep [CODE] in sync with the server's FLEET_CODE.
 */
object FleetConfig {
    const val DOMAIN = "printhub.andreibanu.com"
    const val PATH = "/hersheyhighway"
    const val CODE = "HersheyHighway42069"
}
