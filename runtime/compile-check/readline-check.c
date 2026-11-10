#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <readline/readline.h>
#include <readline/history.h>

int main(void)
{
    FILE *input = tmpfile();
    if (input == NULL) {
        perror("tmpfile");
        return 1;
    }
    /* Exercise readline without waiting for terminal input. */
    if (fputs("xran-ci\n", input) == EOF || fseek(input, 0, SEEK_SET) != 0) {
        perror("prepare readline input");
        fclose(input);
        return 1;
    }
    rl_instream = input;
    char *line = readline("");
    rl_instream = NULL;
    fclose(input);
    if (line == NULL) {
        fputs("readline returned no input\n", stderr);
        return 1;
    }
    int failed = strcmp(line, "xran-ci") != 0;
    using_history();
    add_history(line);
    free(line);
    HIST_ENTRY *entry = history_get(history_base);
    if (history_length != 1 || entry == NULL || strcmp(entry->line, "xran-ci") != 0) {
        fputs("readline history did not preserve input\n", stderr);
        failed = 1;
    }
    clear_history();
    return failed;
}
