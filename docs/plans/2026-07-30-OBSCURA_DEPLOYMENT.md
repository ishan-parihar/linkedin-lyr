# Obscura Backend Deployment Guide

This guide provides detailed instructions for deploying the Obscura backend with gradual rollout strategies.

## Overview

The Obscura backend provides transformative performance improvements over Playwright, but proper deployment requires careful planning to ensure reliability and user experience.

## Pre-Deployment Checklist

### 1. Environment Validation

Before enabling Obscura, ensure your environment meets the requirements:

```bash
# Check if Obscura binary is available
test -f /tmp/obscura && echo "Obscura binary found" || echo "Obscura binary missing"

# Check system memory
free -h

# Check container environment
test -f /.dockerenv && echo "Running in container" || echo "Not in container"
```

### 2. Feature Flag Configuration

Prepare your feature flag configuration:

```bash
# Start with feature flags disabled (default)
export LINKEDIN_OBSURA_ENABLED=false
export LINKEDIN_PLAYWRIGHT_FALLBACK=true
```

### 3. Monitoring Setup

Ensure monitoring is configured:

```bash
# Create log directory
mkdir -p ~/.linkedin-mcp/obscura_logs

# Set up structured logging
export LINKEDIN_LOG_LEVEL=INFO
```

## Deployment Strategies

### Strategy 1: Canary Deployment

Deploy to a small subset of users first:

```bash
# Enable for 10% of users (based on user ID hash)
export LINKEDIN_OBSURA_ENABLED=true
export LINKEDIN_OBSURA_ROLLOUT_PERCENTAGE=10
export LINKEDIN_PLAYWRIGHT_FALLBACK=true
```

Monitor key metrics:
- Error rates
- Response times
- User feedback
- Fallback rates

### Strategy 2: Environment-Based Deployment

Enable Obscura in specific environments:

```bash
# Container environments (ideal for Obscura)
if [ -f /.dockerenv ]; then
    export LINKEDIN_BROWSER_BACKEND=obscura
    export LINKEDIN_PLAYWRIGHT_FALLBACK=true
fi

# Low-memory environments
if [ $(free -m | awk '/MemTotal/{print $2}') -lt 4096 ]; then
    export LINKEDIN_BROWSER_BACKEND=obscura
    export LINKEDIN_PLAYWRIGHT_FALLBACK=true
fi
```

### Strategy 3: Feature Flag Gradual Rollout

Gradually increase rollout percentage:

```bash
# Week 1: 5% rollout
export LINKEDIN_OBSURA_ENABLED=true
export LINKEDIN_OBSURA_ROLLOUT_PERCENTAGE=5

# Week 2: 25% rollout (if metrics are healthy)
export LINKEDIN_OBSURA_ROLLOUT_PERCENTAGE=25

# Week 3: 50% rollout
export LINKEDIN_OBSURA_ROLLOUT_PERCENTAGE=50

# Week 4: 100% rollout
export LINKEDIN_OBSURA_ROLLOUT_PERCENTAGE=100
```

## Configuration Options

### Basic Configuration

```bash
# Backend selection
export LINKEDIN_BROWSER_BACKEND=auto  # Options: playwright, obscura, auto

# Feature flags
export LINKEDIN_OBSURA_ENABLED=true
export LINKEDIN_OBSURA_FORCE=false
export LINKEDIN_PLAYWRIGHT_FALLBACK=true
```

### Advanced Features

```bash
# Connection pooling
export LINKEDIN_OBSURA_CONNECTION_POOLING=true
export LINKEDIN_OBSURA_MAX_CONNECTIONS=5

# Request optimization
export LINKEDIN_OBSURA_CACHING=true
export LINKEDIN_OBSURA_CACHE_TTL=300
export LINKEDIN_OBSURA_BATCH_SIZE=5

# Advanced parsing
export LINKEDIN_OBSURA_ADVANCED_PARSING=true
```

### Monitoring and Debugging

```bash
# Logging
export LINKEDIN_LOG_LEVEL=DEBUG
export LINKEDIN_STRUCTURED_LOGGING=true

# Performance tracking
export LINKEDIN_OBSURA_PERFORMANCE_TRACKING=true
export LINKEDIN_OBSURA_ALERTING=true
```

## Monitoring and Health Checks

### Health Check Script

```bash
#!/bin/bash
# health_check.sh

echo "Checking Obscura health..."

# Check binary
if [ ! -f /tmp/obscura ]; then
    echo "❌ Obscura binary not found"
    exit 1
fi

# Check executable
if [ ! -x /tmp/obscura ]; then
    echo "❌ Obscura binary not executable"
    exit 1
fi

# Check functionality
if ! /tmp/obscura --help > /dev/null 2>&1; then
    echo "❌ Obscura binary not functional"
    exit 1
fi

# Check cookies
if [ ! -f ~/.linkedin-mcp/cookies.json ]; then
    echo "⚠️  Cookie file missing"
fi

echo "✅ Obscura health check passed"
```

### Monitoring Metrics

Key metrics to monitor:

1. **Performance Metrics**
   - Startup time
   - Page fetch duration
   - Memory usage
   - Throughput

2. **Reliability Metrics**
   - Error rate
   - Fallback rate
   - Success rate
   - Timeout rate

3. **User Experience Metrics**
   - Response time
   - Data completeness
   - Parsing accuracy

### Alert Configuration

Set up alerts for:

```bash
# High error rate alert
if [ $error_rate -gt 0.1 ]; then
    alert "High error rate: $error_rate"
fi

# High fallback rate alert
if [ $fallback_rate -gt 0.2 ]; then
    alert "High fallback rate: $fallback_rate"
fi

# Performance regression alert
if [ $latency_p95 -gt $baseline_latency_p95 * 1.2 ]; then
    alert "Performance regression detected"
fi
```

## Rollback Procedures

### Immediate Rollback

```bash
# Disable Obscura immediately
export LINKEDIN_OBSURA_ENABLED=false
export LINKEDIN_BROWSER_BACKEND=playwright

# Restart service
systemctl restart linkedin-mcp-server
```

### Gradual Rollback

```bash
# Reduce rollout percentage
export LINKEDIN_OBSURA_ROLLOUT_PERCENTAGE=10

# Monitor for improvement
# If metrics improve, continue with gradual reduction
# If metrics don't improve, proceed to immediate rollback
```

### Rollback Triggers

Rollback if any of these conditions occur:

- Error rate > 10%
- Fallback rate > 20%
- P95 latency > 2x baseline
- User complaints increase significantly
- Critical functionality breaks

## Troubleshooting

### Common Issues

**Issue**: Obscura binary not found
```bash
# Solution: Ensure Obscura is installed at /tmp/obscura
# Download and install Obscura
```

**Issue**: High fallback rate
```bash
# Solution: Check Obscura health and logs
# Verify cookie validity
# Check network connectivity
```

**Issue**: Performance regression
```bash
# Solution: Check system resources
# Review connection pool settings
# Analyze performance metrics
```

### Debug Mode

Enable debug logging:

```bash
export LINKEDIN_LOG_LEVEL=DEBUG
export LINKEDIN_STRUCTURED_LOGGING=true
export LINKEDIN_OBSURA_DEBUG=true
```

Check logs:

```bash
# View structured logs
tail -f ~/.linkedin-mcp/obscura_logs/obscura_*.jsonl

# View performance metrics
tail -f ~/.linkedin-mcp/obscura_logs/performance_*.jsonl
```

## Validation Steps

### Pre-Deployment Validation

1. **Test Obscura in staging environment**
2. **Validate cookie authentication**
3. **Test all LinkedIn scraping operations**
4. **Verify fallback mechanisms**
5. **Test monitoring and alerting**

### Post-Deployment Validation

1. **Monitor error rates for 24 hours**
2. **Check performance metrics**
3. **Validate data quality**
4. **Review user feedback**
5. **Analyze fallback patterns**

## Success Criteria

Deployment is considered successful when:

- Error rate < 5%
- Fallback rate < 10%
- P95 latency within 20% of baseline
- No critical functionality issues
- Positive user feedback

## Maintenance

### Regular Health Checks

```bash
# Daily health check
./health_check.sh

# Weekly performance review
./performance_review.sh

# Monthly capacity planning
./capacity_planning.sh
```

### Log Rotation

```bash
# Rotate logs weekly
find ~/.linkedin-mcp/obscura_logs -name "*.jsonl" -mtime +7 -delete
```

### Configuration Updates

Review and update configuration regularly:

- Adjust connection pool sizes based on traffic
- Update cache TTL based on content freshness requirements
- Fine-tune batch sizes based on performance metrics
- Review and update alert thresholds

## Support and Resources

For issues or questions:

1. Check the troubleshooting section
2. Review logs and metrics
3. Consult the main README
4. Open an issue on GitHub

## Appendix

### Environment Variables Reference

| Variable | Description | Default |
|----------|-------------|---------|
| `LINKEDIN_BROWSER_BACKEND` | Backend selection (playwright/obscura/auto) | auto |
| `LINKEDIN_OBSURA_ENABLED` | Enable Obscura via feature flag | false |
| `LINKEDIN_OBSURA_FORCE` | Force Obscura mode | false |
| `LINKEDIN_PLAYWRIGHT_FALLBACK` | Enable Playwright fallback | true |
| `LINKEDIN_OBSURA_CONNECTION_POOLING` | Enable connection pooling | false |
| `LINKEDIN_OBSURA_CACHING` | Enable caching | false |
| `LINKEDIN_OBSURA_ADVANCED_PARSING` | Enable advanced parsing | false |
| `LINKEDIN_LOG_LEVEL` | Logging level | INFO |
| `LINKEDIN_STRUCTURED_LOGGING` | Enable structured logging | false |

### Performance Baselines

Based on testing with 10 LinkedIn profile fetches:

| Metric | Playwright | Obscura | Target |
|--------|-----------|---------|--------|
| Startup Time | 2.0s | 0.003s | < 0.01s |
| Memory Usage | 50 MB | 0.02 MB | < 1 MB |
| Page Fetch | 2.5s | 0.829s | < 1.0s |
| Error Rate | 2% | 1% | < 5% |