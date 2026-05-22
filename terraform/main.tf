terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = "us-central1"
}

variable "project_id" {
  default = "greenguard-usa"
}

# ── APIs ──────────────────────────────────────────────────────────────────────

locals {
  apis = [
    "gmail.googleapis.com",
    "calendar-json.googleapis.com",
    "geocoding-backend.googleapis.com",
    "maps-backend.googleapis.com",
    "maps-embed-backend.googleapis.com",
    "distance-matrix-backend.googleapis.com",
    "places-backend.googleapis.com",
    "places.googleapis.com",
    "streetviewpublish.googleapis.com",
    "apikeys.googleapis.com",
    "pubsub.googleapis.com",
    "serviceusage.googleapis.com",
  ]
}

resource "google_project_service" "apis" {
  for_each           = toset(local.apis)
  service            = each.value
  disable_on_destroy = false
}

# ── Unrestricted server-side API key (for scripts + agent) ───────────────────

resource "google_apikeys_key" "server_key" {
  name         = "greenguard-server-key"
  display_name = "GreenGuard Server Key (unrestricted)"
  project      = var.project_id

  restrictions {
    # No restrictions — for server-side use only, never exposed to browser
  }

  depends_on = [google_project_service.apis]
}

# ── Browser-restricted key (for website maps embed) ───────────────────────────

resource "google_apikeys_key" "browser_key" {
  name         = "greenguard-browser-key"
  display_name = "GreenGuard Browser Key (HTTP referrer restricted)"
  project      = var.project_id

  restrictions {
    browser_key_restrictions {
      allowed_referrers = [
        "https://greenguard-usa.com/*",
        "https://www.greenguard-usa.com/*",
        "http://localhost:*/*",
      ]
    }
  }

  depends_on = [google_project_service.apis]
}

# ── Outputs ───────────────────────────────────────────────────────────────────

output "server_api_key" {
  value       = google_apikeys_key.server_key.key_string
  description = "Unrestricted key — use for GOOGLE_MAPS_API_KEY and GOOGLE_API_KEY in .env"
  sensitive   = true
}

output "browser_api_key" {
  value       = google_apikeys_key.browser_key.key_string
  description = "Browser-restricted key — use for website Maps embed"
  sensitive   = true
}
