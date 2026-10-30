# Ephemeral, autoscaled self-hosted GitHub Actions runners on Graviton.
# Each instance registers one just-in-time runner, runs one job and is replaced, so no job sees another's
# workspace. A Lambda (scaler_handler.py, decision in devprod.ci.scaler) sets each pool's desired capacity
# from queued workflow_job events; instances protect themselves from scale-in while a job runs.

data "aws_ssm_parameter" "al2023_arm64" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

data "aws_caller_identity" "me" {}
data "aws_region" "here" {}

resource "aws_security_group" "runner" {
  name_prefix = "${var.name}-"
  vpc_id      = var.vpc_id
  description = "Runners: egress only"
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_s3_bucket" "cache" {
  bucket_prefix = "${var.name}-cache-"
  force_destroy = true
}

resource "aws_s3_object" "jit" {
  bucket = aws_s3_bucket.cache.id
  key    = "bootstrap/jit.py"
  source = "${path.module}/jit.py"
  etag   = filemd5("${path.module}/jit.py")
}

resource "aws_s3_bucket_lifecycle_configuration" "cache" {
  bucket = aws_s3_bucket.cache.id
  rule {
    id     = "expire"
    status = "Enabled"
    filter {
      prefix = "cache/"
    }
    expiration {
      days = var.cache_bucket_days
    }
  }
}

resource "aws_s3_bucket_public_access_block" "cache" {
  bucket                  = aws_s3_bucket.cache.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "runner" {
  name_prefix        = "${var.name}-"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

data "aws_iam_policy_document" "runner" {
  statement {
    sid       = "Cache"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${aws_s3_bucket.cache.arn}/cache/*"]
  }
  statement {
    sid       = "JitConfig"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [var.github_app_secret_arn]
  }
  statement {
    sid       = "Bootstrap"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.cache.arn}/bootstrap/*"]
  }
  statement {
    sid       = "ProtectAndRetireSelf"
    actions   = ["autoscaling:SetInstanceProtection", "autoscaling:TerminateInstanceInAutoScalingGroup"]
    resources = ["arn:aws:autoscaling:${data.aws_region.here.region}:${data.aws_caller_identity.me.account_id}:autoScalingGroup:*:autoScalingGroupName/${var.name}-*"]
  }
}

resource "aws_iam_role_policy" "runner" {
  role   = aws_iam_role.runner.id
  policy = data.aws_iam_policy_document.runner.json
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.runner.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "runner" {
  name_prefix = "${var.name}-"
  role        = aws_iam_role.runner.name
}

resource "aws_launch_template" "pool" {
  for_each      = var.pools
  name_prefix   = "${var.name}-${each.key}-"
  image_id      = data.aws_ssm_parameter.al2023_arm64.value
  instance_type = each.value.instance_types[0]
  iam_instance_profile {
    arn = aws_iam_instance_profile.runner.arn
  }
  vpc_security_group_ids = [aws_security_group.runner.id]
  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }
  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size           = each.value.volume_gb
      volume_type           = "gp3"
      throughput            = 250
      encrypted             = true
      delete_on_termination = true
    }
  }
  user_data = base64encode(templatefile("${path.module}/user_data.sh.tftpl", {
    org        = var.github_org
    labels     = join(",", each.value.labels)
    secret_arn = var.github_app_secret_arn
    cache      = aws_s3_bucket.cache.bucket
    asg        = "${var.name}-${each.key}"
    region     = data.aws_region.here.region
  }))
  tag_specifications {
    resource_type = "instance"
    tags          = { Name = "${var.name}-${each.key}", Pool = each.key }
  }
}

resource "aws_autoscaling_group" "pool" {
  for_each              = var.pools
  name                  = "${var.name}-${each.key}"
  min_size              = 0
  max_size              = each.value.max
  desired_capacity      = each.value.min
  vpc_zone_identifier   = var.subnet_ids
  capacity_rebalance    = true
  protect_from_scale_in = false
  mixed_instances_policy {
    instances_distribution {
      on_demand_base_capacity                  = each.value.min
      on_demand_percentage_above_base_capacity = floor((1 - var.spot_share) * 100)
      spot_allocation_strategy                 = "price-capacity-optimized"
    }
    launch_template {
      launch_template_specification {
        launch_template_id = aws_launch_template.pool[each.key].id
        version            = "$Latest"
      }
      dynamic "override" {
        for_each = each.value.instance_types
        content {
          instance_type = override.value
        }
      }
    }
  }
  lifecycle {
    ignore_changes = [desired_capacity] # the scaler owns it
  }
}

# ---- the scaler

resource "aws_sqs_queue" "jobs_dlq" {
  name_prefix               = "${var.name}-jobs-dlq-"
  message_retention_seconds = 1209600
}

resource "aws_sqs_queue" "jobs" {
  name_prefix                = "${var.name}-jobs-"
  visibility_timeout_seconds = 60
  redrive_policy             = jsonencode({ deadLetterTargetArn = aws_sqs_queue.jobs_dlq.arn, maxReceiveCount = 5 })
}

data "archive_file" "scaler" {
  type        = "zip"
  output_path = "${path.module}/.build/scaler.zip"
  source {
    content  = file("${path.module}/scaler_handler.py")
    filename = "scaler_handler.py"
  }
  source {
    content  = file("${path.module}/../../src/devprod/ci/scaler.py")
    filename = "devprod/ci/scaler.py"
  }
  source {
    content  = ""
    filename = "devprod/__init__.py"
  }
  source {
    content  = ""
    filename = "devprod/ci/__init__.py"
  }
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "scaler" {
  name_prefix        = "${var.name}-scaler-"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "scaler" {
  statement {
    actions   = ["autoscaling:DescribeAutoScalingGroups"]
    resources = ["*"]
  }
  statement {
    actions   = ["autoscaling:SetDesiredCapacity"]
    resources = [for g in aws_autoscaling_group.pool : g.arn]
  }
  statement {
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
    resources = [aws_sqs_queue.jobs.arn]
  }
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.scaler.arn}:*"]
  }
}

resource "aws_iam_role_policy" "scaler" {
  role   = aws_iam_role.scaler.id
  policy = data.aws_iam_policy_document.scaler.json
}

resource "aws_cloudwatch_log_group" "scaler" {
  name              = "/aws/lambda/${var.name}-scaler"
  retention_in_days = 30
}

resource "aws_lambda_function" "scaler" {
  function_name                  = "${var.name}-scaler"
  role                           = aws_iam_role.scaler.arn
  runtime                        = "python3.13"
  architectures                  = ["arm64"]
  handler                        = "scaler_handler.handler"
  filename                       = data.archive_file.scaler.output_path
  source_code_hash               = data.archive_file.scaler.output_base64sha256
  timeout                        = 30
  memory_size                    = 256
  reserved_concurrent_executions = 1 # one decision at a time
  environment {
    variables = {
      SCALER_CONFIG = var.scaler_config
      POOLS         = jsonencode({ for k, p in var.pools : "${var.name}-${k}" => p.labels })
    }
  }
  depends_on = [aws_cloudwatch_log_group.scaler]
}

resource "aws_lambda_event_source_mapping" "jobs" {
  event_source_arn                   = aws_sqs_queue.jobs.arn
  function_name                      = aws_lambda_function.scaler.arn
  batch_size                         = 100
  maximum_batching_window_in_seconds = 5
}

resource "aws_scheduler_schedule" "tick" {
  name                = "${var.name}-tick"
  schedule_expression = "rate(1 minute)"
  flexible_time_window {
    mode = "OFF"
  }
  target {
    arn      = aws_lambda_function.scaler.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ Records = [] })
  }
}

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name_prefix        = "${var.name}-tick-"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

resource "aws_iam_role_policy" "scheduler" {
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "lambda:InvokeFunction", Resource = aws_lambda_function.scaler.arn }]
  })
}

output "jobs_queue_url" {
  description = "Where the webhook receiver puts verified workflow_job events"
  value       = aws_sqs_queue.jobs.url
}

output "cache_bucket" {
  value = aws_s3_bucket.cache.bucket
}
