# Terraform skeleton (Phase 1 placeholder).
#
# This directory intentionally provisions nothing yet. Add cloud/provider
# resources in the deployment phase, with remote state, encryption, and no
# secrets in source or tfvars.

terraform {
  required_version = ">= 1.9.0"

  # Remote backend is configured in the deployment phase. Example shape only:
  # backend "s3" {
  #   bucket         = "<state-bucket>"
  #   key            = "ai-api-gateway-saas/terraform.tfstate"
  #   region         = "<region>"
  #   encrypt        = true
  #   dynamodb_table = "<lock-table>"
  # }

  # Providers are selected in the deployment phase. Do not commit credentials.
  # required_providers {
  #   aws = {
  #     source  = "hashicorp/aws"
  #     version = "~> 5.0"
  #   }
  # }
}
