BEGIN {
    RS="\n|\r";
    IGNORECASE=1;
}

# Remove comments
{sub(/#.*$/, "", $1)}

#Strip spaces
{sub(/\s/, "", $1)}

#Strip double qoutes
{sub(/"/, "", $1)}

# Skipping empty strings
(!$1) {next}

# Skipping IP addresses
(/^([0-9]{1,3}\.){3}[0-9]{1,3}$/) {next}

# Removing leading "www."
{sub(/^www\./, "", $1)}

# Removing non letter symbols after domain
{sub(/[^а-яА-Яa-zA-Z]+$/, "", $1)}

{print $1}
