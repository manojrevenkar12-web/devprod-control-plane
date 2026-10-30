variable "name" {
  description = "Prefix for every resource"
  type        = string
  default     = "devprod-runners"
}

variable "vpc_id" {
  type = string
}

variable "subnet_ids" {
  description = "Private subnets for the runners (egress through NAT)"
  type        = list(string)
}

variable "github_org" {
  type = string
}

variable "github_app_secret_arn" {
  description = "Secrets Manager secret holding the GitHub App id and private key used to mint just-in-time runner configs"
  type        = string
}

variable "pools" {
  description = "One Auto Scaling group per label set. Labels must match runs-on in the generated workflows."
  type = map(object({
    labels         = list(string)
    instance_types = list(string)
    min            = number
    max            = number
    volume_gb      = number
  }))
  default = {
    python-medium = { labels = ["self-hosted", "linux", "arm64", "ci-python", "medium"], instance_types = ["c7g.xlarge", "c8g.xlarge", "m7g.xlarge"], min = 4, max = 48, volume_gb = 60 }
    python-small  = { labels = ["self-hosted", "linux", "arm64", "ci-python", "small"], instance_types = ["c7g.large", "c8g.large"], min = 1, max = 12, volume_gb = 30 }
    ts-medium     = { labels = ["self-hosted", "linux", "arm64", "ci-typescript", "medium"], instance_types = ["c7g.xlarge", "c8g.xlarge"], min = 1, max = 16, volume_gb = 60 }
  }
}

variable "scaler_config" {
  description = "The fleet section of config/ci.yaml as JSON (target queue, idle shutdown, min, max)"
  type        = string
}

variable "spot_share" {
  description = "Share of capacity above the on-demand base that runs on Spot (jobs are retried by the triage agent on interruption)"
  type        = number
  default     = 0.8
}

variable "cache_bucket_days" {
  type    = number
  default = 14
}
