# ghpr shell integration for fish
# Add to your config.fish: source path/to/ghpr.fish
# Or use: ghpr shell-integration fish | source

# Core ghpr aliases for common operations
function ghpri                         # initialize new PR draft and cd into it (gh/new by default; pass slug for gh/new-<slug>)
    set output (ghpr init $argv)
    set init_status $status
    set dir (echo "$output" | grep "^GHPR_DIR:" | sed 's/^GHPR_DIR://')
    if test $init_status -eq 0 -a -n "$dir" -a -d "$dir"
        cd $dir
    end
end
alias ghpro='ghpr open'                # open existing PR in browser
alias ghprog='ghpr open -g'            # open gist in browser
alias ghprcr='ghpr create'             # create new PR from description
alias ghprcrn='ghpr create -n'         # dry-run: show what PR would be created
alias ghprsh='ghpr show'               # show PR and gist URLs
alias ghprshg='ghpr show -g'           # show only gist URL
function ghprc                         # clone PR and cd into directory
    set output (ghpr clone $argv)
    set clone_status $status
    set dir (echo "$output" | grep "^GHPR_DIR:" | sed 's/^GHPR_DIR://')
    if test $clone_status -eq 0 -a -n "$dir" -a -d "$dir"
        cd $dir
    end
end
alias ghprp='ghpr push'                # push to PR (auto-adds footer if gist exists)
alias ghprpn='ghpr push -n'            # dry-run push
alias ghprl='ghpr pull'                # pull from PR (and optionally push back)
alias ghprln='ghpr pull -n'            # dry-run pull (no local or remote changes)
alias ghprf='ghpr fetch'               # snapshot GitHub into github/remote only
alias ghprfn='ghpr fetch -n'           # dry-run fetch
alias ghprpg='ghpr push -g'            # push with gist backup (auto-footer)
alias ghprpo='ghpr push -o'            # push and open in browser
alias ghprpF='ghpr push -F'            # push WITHOUT footer
alias ghprd='ghpr diff'                # diff local vs remote PR
alias ghpria='ghpr ingest-attachments' # ingest user-attachments from PR
alias ghia='ghpr ingest-attachments'   # short alias for ingest-attachments
alias ghpru='ghpr upload'              # upload images to PR's gist
