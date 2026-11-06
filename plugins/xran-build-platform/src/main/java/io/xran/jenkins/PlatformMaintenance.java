package io.xran.jenkins;

import hudson.Extension;
import hudson.model.PeriodicWork;
import hudson.security.ACL;

/** Repairs stale leases and any cloud/lock drift after a complete global form save. */
@Extension
public final class PlatformMaintenance extends PeriodicWork {
    @Override public long getRecurrencePeriod() { return 5000L; }

    @Override protected void doRun() throws Exception {
        try (var ignored = ACL.as2(ACL.SYSTEM2)) {
            BuildPlatformConfiguration.get().maintain();
        }
    }
}
