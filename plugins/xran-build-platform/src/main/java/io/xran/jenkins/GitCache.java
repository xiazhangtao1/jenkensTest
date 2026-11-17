package io.xran.jenkins;

import java.io.IOException;
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.nio.file.FileVisitResult;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.nio.file.SimpleFileVisitor;
import java.nio.file.attribute.BasicFileAttributes;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.concurrent.TimeUnit;
import java.util.regex.Pattern;
import net.sf.json.JSONObject;

/** Rebuildable cache outside HOME. Writers and eviction use the same Linux flock. */
public final class GitCache {
    private static final Pattern PROJECT = Pattern.compile("[a-z0-9][a-z0-9-]{0,61}");
    private static final Pattern ENTRY = Pattern.compile("[a-f0-9]{64}\\.(?:git|json|tmp-[A-Za-z0-9_-]+)");
    private final Path root;
    public GitCache(Path root) { this.root = root == null ? null : root.toAbsolutePath().normalize(); }
    public static GitCache get() {
        String path = System.getProperty("xran.gitCacheRoot", System.getenv("XRAN_GIT_CACHE_ROOT"));
        return new GitCache(path == null ? null : Path.of(path));
    }
    public boolean available() { return root != null && Files.isDirectory(root, LinkOption.NOFOLLOW_LINKS); }
    private Path project(String id) throws IOException {
        if (id == null || !PROJECT.matcher(id).matches() || root == null) throw new IOException("非法 Git 缓存项目或 PVC 未挂载");
        Path directory = root.resolve(id);
        if (Files.isSymbolicLink(root) || Files.isSymbolicLink(directory)) throw new IOException("缓存路径不能是符号链接");
        return directory;
    }
    public void initialize(List<String> projects) throws IOException {
        if (root == null) return; // Older deployments and unit tests have no cache volume.
        if (!available()) throw new IOException("Git 缓存 PVC 未挂载：" + root);
        for (String id : projects) {
            Path directory = project(id);
            Files.createDirectories(directory);
            Files.setAttribute(directory, "unix:mode", 02775);
            Path lock = directory.resolve(".lock");
            if (Files.isSymbolicLink(lock)) throw new IOException("非法缓存锁文件");
            try { Files.createFile(lock); }
            catch (java.nio.file.FileAlreadyExistsException existing) {
                if (!Files.isRegularFile(lock, LinkOption.NOFOLLOW_LINKS)) throw new IOException("缓存锁不是普通文件", existing);
            }
            Files.setAttribute(lock, "unix:mode", 0664);
        }
    }
    public long device() throws IOException { return ((Number) Files.getAttribute(root, "unix:dev")).longValue(); }
    public long usableSpace() { return root.toFile().getUsableSpace(); }
    public long totalSpace() { return root.toFile().getTotalSpace(); }
    private static long size(Path directory) throws IOException {
        long[] bytes = {0};
        Files.walkFileTree(directory, new SimpleFileVisitor<>() {
            @Override public FileVisitResult visitFile(Path file, BasicFileAttributes attributes) {
                if (attributes.isRegularFile()) bytes[0] += attributes.size();
                return FileVisitResult.CONTINUE;
            }
            @Override public FileVisitResult visitFileFailed(Path file, IOException failure) throws IOException {
                if (failure instanceof java.nio.file.NoSuchFileException) return FileVisitResult.CONTINUE;
                throw failure;
            }
        });
        return bytes[0];
    }
    private static JSONObject metadata(Path path) throws IOException {
        if (Files.isSymbolicLink(path)) throw new IOException("非法缓存状态文件");
        if (!Files.isRegularFile(path, LinkOption.NOFOLLOW_LINKS)) return new JSONObject();
        try { return JSONObject.fromObject(Files.readString(path)); }
        catch (java.nio.file.NoSuchFileException removed) { return new JSONObject(); }
        catch (RuntimeException malformed) {
            JSONObject result = new JSONObject();
            result.put("lastError", "状态文件损坏，可清空后重建");
            return result;
        }
    }
    private List<Map<String, Object>> entries(boolean refresh) throws IOException {
        List<Map<String, Object>> result = new ArrayList<>();
        if (!available()) return result;
        try (var projects = Files.list(root)) {
            for (Path directory : projects.toList()) {
                String id = Objects.requireNonNull(directory.getFileName()).toString();
                if (!PROJECT.matcher(id).matches()) continue;
                project(id);
                if (!Files.isDirectory(directory, LinkOption.NOFOLLOW_LINKS)) continue;
                try (var children = Files.list(directory)) {
                    List<Path> paths = children.toList();
                    for (Path entry : paths) {
                        String name = Objects.requireNonNull(entry.getFileName()).toString();
                        if (!ENTRY.matcher(name).matches()) continue;
                        if (Files.isSymbolicLink(entry)) throw new IOException("非法缓存条目：" + entry);
                        boolean stateOnly = name.endsWith(".json");
                        String key = name.substring(0, 64);
                        if (stateOnly && paths.stream().anyMatch(path -> {
                            String other = Objects.requireNonNull(path.getFileName()).toString();
                            return other.equals(key + ".git") || other.startsWith(key + ".tmp-");
                        })) continue;
                        if (!stateOnly && !Files.isDirectory(entry, LinkOption.NOFOLLOW_LINKS)) continue;
                        JSONObject saved = metadata(directory.resolve(key + ".json"));
                        Map<String, Object> value = new LinkedHashMap<>();
                        value.put("project", id); value.put("entry", name);
                        value.put("bytes", stateOnly ? 0L : refresh || !saved.containsKey("bytes") ? size(entry) : saved.optLong("bytes"));
                        value.put("lastUsedAt", saved.optDouble("lastUsedAt", 0));
                        value.put("lastUpdatedAt", saved.optString("lastUpdatedAt", ""));
                        value.put("status", saved.optString("status", "incomplete"));
                        value.put("lastError", saved.optString("lastError", ""));
                        value.put("commit", saved.optString("commit", ""));
                        result.add(value);
                    }
                }
            }
        }
        result.sort(Comparator.comparingDouble(value -> ((Number) value.get("lastUsedAt")).doubleValue()));
        return result;
    }
    public Map<String, Object> status(long budget) throws IOException {
        var entries = entries(false);
        long total = entries.stream().mapToLong(value -> ((Number) value.get("bytes")).longValue()).sum();
        return Map.of("available", available(), "budgetBytes", budget, "bytes", total,
                "overBudget", total > budget, "entries", entries);
    }
    @FunctionalInterface private interface Work { void run() throws IOException; }
    private boolean locked(String id, Work work) throws IOException {
        Path directory = project(id), lock = directory.resolve(".lock");
        if (Files.isSymbolicLink(lock) || !Files.isRegularFile(lock)) throw new IOException("缓存锁不可用");
        // Keep the kernel flock in a tiny child while Java enumerates/deletes.
        // Closing stdin releases it, including if the controller exits.
        Process process = new ProcessBuilder("/usr/bin/flock", "-E", "75", "-n", lock.toString(),
                "/bin/sh", "-c", "printf 'locked\\n'; read -r release").redirectErrorStream(true).start();
        try (var control = process.getOutputStream();
             var reader = new BufferedReader(new InputStreamReader(process.getInputStream(), java.nio.charset.StandardCharsets.UTF_8))) {
            String message = reader.readLine();
            if (!"locked".equals(message)) {
                if (process.waitFor(5, TimeUnit.SECONDS) && process.exitValue() == 75) return false;
                throw new IOException("Git 缓存锁失败：" + message);
            }
            work.run();
            return true;
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
            throw new IOException("Git 缓存清理被中断", interrupted);
        } finally {
            try { if (!process.waitFor(5, TimeUnit.SECONDS)) process.destroyForcibly(); }
            catch (InterruptedException interrupted) { process.destroyForcibly(); Thread.currentThread().interrupt(); }
        }
    }

    private static void delete(Path path) throws IOException {
        if (Files.isSymbolicLink(path)) throw new IOException("缓存清理路径不能是符号链接");
        if (!Files.exists(path, LinkOption.NOFOLLOW_LINKS)) return;
        Files.walkFileTree(path, new SimpleFileVisitor<>() {
            @Override public FileVisitResult visitFile(Path file, BasicFileAttributes attrs) throws IOException {
                Files.delete(file); return FileVisitResult.CONTINUE;
            }
            @Override public FileVisitResult postVisitDirectory(Path directory, IOException error) throws IOException {
                if (error != null) throw error;
                Files.delete(directory); return FileVisitResult.CONTINUE;
            }
        });
    }
    public boolean clear(String id) throws IOException {
        Path directory = project(id);
        return locked(id, () -> {
            try (var children = Files.list(directory)) {
                for (Path path : children.toList()) {
                    String name = path.getFileName().toString();
                    if (ENTRY.matcher(name).matches() || name.matches("[a-f0-9]{64}\\.json")) delete(path);
                }
            }
        });
    }
    /** Called after historical artifact reclamation. Busy projects are skipped. */
    public Map<String, Object> enforce(long budget, Map<String, Long> requiredFree) throws IOException {
        List<Map<String, Object>> removed = new ArrayList<>();
        List<String> busy = new ArrayList<>();
        if (!available()) return Map.of("deleted", removed, "busy", busy, "bytes", 0L);
        var entries = entries(true);
        long total = entries.stream().mapToLong(value -> ((Number) value.get("bytes")).longValue()).sum();
        long target = requiredFree.getOrDefault(Long.toString(device()), 0L);
        for (var entry : entries) {
            if (total <= budget && usableSpace() >= target) break;
            String id = entry.get("project").toString(), name = entry.get("entry").toString();
            Path directory = project(id);
            String reason = total > budget ? "cache-budget" : "disk-pressure";
            long[] reclaimed = {-1};
            if (!locked(id, () -> {
                Path path = directory.resolve(name);
                if (!Files.exists(path, LinkOption.NOFOLLOW_LINKS)) return;
                JSONObject latest = metadata(directory.resolve(name.substring(0, 64) + ".json"));
                // The scan may have preceded a completed checkout. Keep its new
                // recency rather than evicting it with an obsolete LRU value.
                if (latest.optDouble("lastUsedAt", 0) > ((Number) entry.get("lastUsedAt")).doubleValue()) return;
                reclaimed[0] = name.endsWith(".json") ? 0L : size(path);
                delete(path);
                if (name.endsWith(".git") || !Files.exists(directory.resolve(name.substring(0, 64) + ".git"))) {
                    delete(directory.resolve(name.substring(0, 64) + ".json"));
                }
            })) {
                busy.add(id); continue;
            }
            if (reclaimed[0] < 0) continue;
            total -= ((Number) entry.get("bytes")).longValue();
            removed.add(Map.of("at", Instant.now().toString(), "project", id, "entry", name,
                    "bytes", reclaimed[0], "reason", reason, "availableAfter", usableSpace()));
        }
        return Map.of("deleted", removed, "busy", busy, "bytes", total, "budgetBytes", budget,
                "overBudget", total > budget, "available", usableSpace(), "minimum", target);
    }
}
