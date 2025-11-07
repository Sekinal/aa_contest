"""Proxy pool management with IP blocking detection and cooldown"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set
from collections import defaultdict

from loguru import logger


@dataclass
class ProxyConfig:
    """Configuration for a single proxy"""
    host: str
    port: int
    username: str
    password: str
    id: int
    
    # Health tracking
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    blocked_count: int = 0
    
    # Cooldown management
    is_cooling_down: bool = False
    cooldown_until: Optional[datetime] = None
    cooldown_reason: Optional[str] = None
    
    # Browser assignments
    assigned_browsers: Set[int] = field(default_factory=set)
    max_browsers: int = 3  # Safe limit per proxy
    
    def to_url(self) -> str:
        """Convert to proxy URL format for httpx/playwright"""
        return f"http://{self.username}:{self.password}@{self.host}:{self.port}"
    
    def to_playwright_dict(self) -> Dict[str, str]:
        """Convert to Playwright proxy format"""
        return {
            "server": f"http://{self.host}:{self.port}",
            "username": self.username,
            "password": self.password,
        }
    
    def mark_blocked(self, duration_minutes: int = 40) -> None:
        """Mark proxy as blocked and set cooldown"""
        self.is_cooling_down = True
        self.cooldown_until = datetime.now() + timedelta(minutes=duration_minutes)
        self.cooldown_reason = "IP blocked by server"
        self.blocked_count += 1
        
        logger.warning(
            f"🚫 Proxy #{self.id} ({self.host}:{self.port}) marked as BLOCKED"
        )
        logger.warning(
            f"   Cool down until: {self.cooldown_until.strftime('%H:%M:%S')}"
        )
    
    def check_cooldown(self) -> bool:
        """Check if cooldown period has expired"""
        if not self.is_cooling_down:
            return True
        
        if self.cooldown_until and datetime.now() >= self.cooldown_until:
            self.is_cooling_down = False
            self.cooldown_until = None
            self.cooldown_reason = None
            logger.info(f"✅ Proxy #{self.id} cooldown expired - back in rotation")
            return True
        
        return False
    
    def can_assign_browser(self) -> bool:
        """Check if proxy can accept another browser"""
        return (
            not self.is_cooling_down and 
            len(self.assigned_browsers) < self.max_browsers
        )
    
    def get_success_rate(self) -> float:
        """Calculate success rate percentage"""
        if self.total_requests == 0:
            return 0.0
        return (self.successful_requests / self.total_requests) * 100
    
    def __str__(self) -> str:
        status = "🔴 COOLING" if self.is_cooling_down else "🟢 ACTIVE"
        return (
            f"Proxy #{self.id} {status} - {self.host}:{self.port} "
            f"[Browsers: {len(self.assigned_browsers)}/{self.max_browsers}] "
            f"[Success: {self.get_success_rate():.1f}%]"
        )


class ProxyPool:
    """
    Manages proxy rotation with IP block detection and cooldown.
    
    Features:
    - Automatic proxy rotation
    - IP block detection and cooldown management
    - Load balancing across proxies
    - Health tracking per proxy
    - Up to 3 browsers per proxy (safe limit)
    """
    
    def __init__(
        self,
        proxy_file: Path,
        cooldown_minutes: int = 40,
        max_browsers_per_proxy: int = 3,
    ):
        """
        Initialize proxy pool from file.
        
        Args:
            proxy_file: Path to proxy file (format: host:port:username:password per line)
            cooldown_minutes: Minutes to wait after IP block (default: 40)
            max_browsers_per_proxy: Max browsers per proxy (default: 3)
        """
        self.proxy_file = proxy_file
        self.cooldown_minutes = cooldown_minutes
        self.max_browsers_per_proxy = max_browsers_per_proxy
        
        self.proxies: List[ProxyConfig] = []
        self.current_index = 0
        self.lock = asyncio.Lock()
        
        # Load proxies from file
        self._load_proxies()
        
        logger.info(f"🌐 Proxy pool initialized:")
        logger.info(f"   Total proxies: {len(self.proxies)}")
        logger.info(f"   Cooldown period: {cooldown_minutes} minutes")
        logger.info(f"   Max browsers per proxy: {max_browsers_per_proxy}")
        logger.info(f"   Total browser capacity: {len(self.proxies) * max_browsers_per_proxy}")
    
    def _load_proxies(self) -> None:
        """Load proxies from file"""
        if not self.proxy_file.exists():
            raise FileNotFoundError(f"Proxy file not found: {self.proxy_file}")
        
        lines = self.proxy_file.read_text().strip().split('\n')
        
        for i, line in enumerate(lines):
            line = line.strip()
            
            # Skip empty lines and comments
            if not line or line.startswith('#'):
                continue
            
            # Parse proxy line: host:port:username:password
            parts = line.split(':')
            if len(parts) != 4:
                logger.warning(f"Skipping invalid proxy line {i+1}: {line}")
                continue
            
            host, port, username, password = parts
            
            try:
                proxy = ProxyConfig(
                    host=host.strip(),
                    port=int(port.strip()),
                    username=username.strip(),
                    password=password.strip(),
                    id=i,
                    max_browsers=self.max_browsers_per_proxy,
                )
                self.proxies.append(proxy)
                logger.debug(f"   Loaded: {host}:{port}")
            except ValueError as e:
                logger.warning(f"Skipping invalid proxy line {i+1}: {e}")
        
        if not self.proxies:
            raise ValueError("No valid proxies found in file")
        
        logger.success(f"✅ Loaded {len(self.proxies)} proxies")
    
    async def get_available_proxy(self, browser_id: Optional[int] = None) -> Optional[ProxyConfig]:
        """
        Get next available proxy with round-robin + health-based selection.
        
        Args:
            browser_id: Optional browser ID to track assignment
            
        Returns:
            ProxyConfig if available, None if all proxies are cooling down
        """
        async with self.lock:
            # Check cooldowns
            for proxy in self.proxies:
                proxy.check_cooldown()
            
            # Get list of available proxies
            available = [
                p for p in self.proxies 
                if p.can_assign_browser()
            ]
            
            if not available:
                # All proxies cooling down - find next to expire
                cooling = [p for p in self.proxies if p.is_cooling_down]
                if cooling:
                    next_available = min(cooling, key=lambda p: p.cooldown_until)
                    wait_seconds = (next_available.cooldown_until - datetime.now()).total_seconds()
                    logger.error(
                        f"⚠️ All proxies cooling down! Next available in {wait_seconds/60:.1f} minutes"
                    )
                return None
            
            # Sort by:
            # 1. Fewest assigned browsers (load balance)
            # 2. Highest success rate (prefer healthy proxies)
            # 3. Fewest total requests (spread load)
            available.sort(
                key=lambda p: (
                    len(p.assigned_browsers),
                    -p.get_success_rate() if p.total_requests > 0 else 0,
                    p.total_requests
                )
            )
            
            proxy = available[0]
            
            if browser_id is not None:
                proxy.assigned_browsers.add(browser_id)
            
            logger.debug(
                f"🌐 Assigned {proxy.host}:{proxy.port} "
                f"(browsers: {len(proxy.assigned_browsers)}/{proxy.max_browsers})"
            )
            
            return proxy
    
    async def mark_proxy_blocked(self, proxy: ProxyConfig) -> None:
        """
        Mark a proxy as blocked and clear its browser assignments.
        
        Args:
            proxy: Proxy that was blocked
        """
        async with self.lock:
            proxy.mark_blocked(duration_minutes=self.cooldown_minutes)
            
            # Clear browser assignments (they'll need to get new proxies)
            assigned_count = len(proxy.assigned_browsers)
            proxy.assigned_browsers.clear()
            
            logger.warning(
                f"   Cleared {assigned_count} browser assignments from blocked proxy"
            )
            
            # Log remaining capacity
            active_proxies = sum(1 for p in self.proxies if not p.is_cooling_down)
            total_capacity = active_proxies * self.max_browsers_per_proxy
            
            if active_proxies == 0:
                logger.error("🚨 NO ACTIVE PROXIES - All proxies are cooling down!")
            else:
                logger.info(
                    f"   Active proxies: {active_proxies}/{len(self.proxies)} "
                    f"(capacity: {total_capacity} browsers)"
                )
    
    async def mark_proxy_success(self, proxy: ProxyConfig) -> None:
        """Record successful request for proxy"""
        async with self.lock:
            proxy.total_requests += 1
            proxy.successful_requests += 1
    
    async def mark_proxy_failure(self, proxy: ProxyConfig, is_block: bool = False) -> None:
        """Record failed request for proxy"""
        async with self.lock:
            proxy.total_requests += 1
            proxy.failed_requests += 1
            
            if is_block:
                await self.mark_proxy_blocked(proxy)
    
    def get_stats(self) -> Dict:
        """Get comprehensive proxy pool statistics"""
        active = [p for p in self.proxies if not p.is_cooling_down]
        cooling = [p for p in self.proxies if p.is_cooling_down]
        
        total_requests = sum(p.total_requests for p in self.proxies)
        total_success = sum(p.successful_requests for p in self.proxies)
        total_blocks = sum(p.blocked_count for p in self.proxies)
        
        return {
            "total_proxies": len(self.proxies),
            "active_proxies": len(active),
            "cooling_proxies": len(cooling),
            "total_capacity": len(active) * self.max_browsers_per_proxy,
            "total_requests": total_requests,
            "total_successful": total_success,
            "total_blocks": total_blocks,
            "overall_success_rate": (total_success / total_requests * 100) if total_requests > 0 else 0,
            "proxies": [
                {
                    "id": p.id,
                    "host": f"{p.host}:{p.port}",
                    "status": "cooling" if p.is_cooling_down else "active",
                    "browsers": len(p.assigned_browsers),
                    "requests": p.total_requests,
                    "success_rate": p.get_success_rate(),
                    "blocks": p.blocked_count,
                    "cooldown_until": p.cooldown_until.isoformat() if p.cooldown_until else None,
                }
                for p in self.proxies
            ]
        }
    
    def print_stats(self) -> None:
        """Print formatted proxy statistics"""
        stats = self.get_stats()
        
        logger.info("")
        logger.info("=" * 80)
        logger.info("🌐 PROXY POOL STATISTICS")
        logger.info("=" * 80)
        
        logger.info(f"Total Proxies:         {stats['total_proxies']}")
        logger.info(f"✅ Active:             {stats['active_proxies']}")
        logger.info(f"🔴 Cooling Down:       {stats['cooling_proxies']}")
        logger.info(f"Total Capacity:        {stats['total_capacity']} browsers")
        logger.info("")
        logger.info(f"Total Requests:        {stats['total_requests']}")
        logger.info(f"Successful:            {stats['total_successful']}")
        logger.info(f"Success Rate:          {stats['overall_success_rate']:.1f}%")
        logger.info(f"Total IP Blocks:       {stats['total_blocks']}")
        logger.info("")
        logger.info("Per-Proxy Breakdown:")
        
        for proxy_stat in stats['proxies']:
            status_icon = "🟢" if proxy_stat['status'] == 'active' else "🔴"
            logger.info(
                f"  {status_icon} Proxy #{proxy_stat['id']} ({proxy_stat['host']}): "
                f"{proxy_stat['requests']} req, {proxy_stat['success_rate']:.1f}% success, "
                f"{proxy_stat['blocks']} blocks, {proxy_stat['browsers']} browsers"
            )
            if proxy_stat['cooldown_until']:
                logger.info(f"     Cooldown until: {proxy_stat['cooldown_until']}")
        
        logger.info("=" * 80)