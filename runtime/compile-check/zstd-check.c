#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <zstd.h>

int main(void)
{
    const char input[] = "X-RAN-CLOUD zstd compiler environment check";
    char output[sizeof(input)];
    const size_t capacity = ZSTD_compressBound(sizeof(input));
    void *compressed = malloc(capacity);
    if (compressed == NULL)
        return 1;

    const size_t size = ZSTD_compress(compressed, capacity, input, sizeof(input), 1);
    if (ZSTD_isError(size)) {
        fprintf(stderr, "ZSTD_compress: %s\n", ZSTD_getErrorName(size));
        free(compressed);
        return 2;
    }
    const size_t decoded = ZSTD_decompress(output, sizeof(output), compressed, size);
    free(compressed);
    if (ZSTD_isError(decoded)) {
        fprintf(stderr, "ZSTD_decompress: %s\n", ZSTD_getErrorName(decoded));
        return 3;
    }
    if (decoded != sizeof(input) || memcmp(input, output, sizeof(input)) != 0)
        return 4;

    printf("zstd %s: header, link and compression round trip OK\n", ZSTD_versionString());
    return 0;
}
