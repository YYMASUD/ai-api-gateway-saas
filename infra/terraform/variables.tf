variable "project_name" {
  description = "Deployment project slug."
  type        = string
  default     = "ai-api-gateway-saas"
}

variable "environment" {
  description = "Deployment environment name (for example staging or production)."
  type        = string
  default     = "staging"
}

variable "tags" {
  description = "Common resource tags."
  type        = map(string)
  default     = {}
}
