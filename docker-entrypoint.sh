#!/bin/sh
set -eu

INSTALL_DIR="${INSTALL_DIR:-/opt/pymc_repeater}"
CONFIG_DIR="${CONFIG_DIR:-/etc/pymc_repeater}"
CONFIG_PATH="${PYMC_REPEATER_CONFIG:-${CONFIG_DIR}/config.yaml}"
EXAMPLE_PATH="${CONFIG_DIR}/config.yaml.example"
BUNDLED_EXAMPLE_PATH="${INSTALL_DIR}/config.yaml.example"
RUNTIME_USER="${USER:-repeater}"
RUNTIME_UID="${PUID:-unknown}"
RUNTIME_GID="${PGID:-unknown}"
YQ_CMD="${YQ_CMD:-/usr/local/bin/yq}"

mkdir -p "${CONFIG_DIR}"

copy_or_die() {
    src="$1"
    dest="$2"
    if ! cp "${src}" "${dest}"; then
        echo "Failed to initialize ${dest} from ${src}." >&2
        echo "If you are bind-mounting ./config.yaml, ensure the host path is writable by ${RUNTIME_USER} (${RUNTIME_UID}:${RUNTIME_GID})." >&2
        exit 1
    fi
}

merge_config_from_example() {
    config_path="$1"

    if [ ! -f "${config_path}" ] || [ ! -f "${EXAMPLE_PATH}" ]; then
        return 0
    fi

    if [ ! -x "${YQ_CMD}" ] || ! "${YQ_CMD}" --version 2>&1 | grep -q "mikefarah/yq"; then
        echo "Skipping config merge: mikefarah yq is not available at ${YQ_CMD}." >&2
        return 0
    fi

    tmpdir="$(mktemp -d)"
    stripped_user="${tmpdir}/config.stripped.yaml"
    merged_config="${tmpdir}/config.merged.yaml"

    cleanup_merge() {
        rm -rf "${tmpdir}"
    }
    trap cleanup_merge EXIT HUP INT TERM

    # Keep only the example's comments to avoid comment duplication across upgrades.
    "${YQ_CMD}" eval '... comments=""' "${config_path}" > "${stripped_user}" 2>/dev/null || cp "${config_path}" "${stripped_user}"

    if ! "${YQ_CMD}" eval-all '. as $item ireduce ({}; . * $item)' "${EXAMPLE_PATH}" "${stripped_user}" > "${merged_config}" 2>/dev/null; then
        echo "Failed to merge ${config_path} with ${EXAMPLE_PATH}; keeping the existing config." >&2
        cleanup_merge
        trap - EXIT HUP INT TERM
        return 0
    fi

    if ! "${YQ_CMD}" eval '.' "${merged_config}" >/dev/null 2>&1; then
        echo "Merged config for ${config_path} is invalid; keeping the existing config." >&2
        cleanup_merge
        trap - EXIT HUP INT TERM
        return 0
    fi

    if ! cmp -s "${config_path}" "${merged_config}"; then
        copy_or_die "${merged_config}" "${config_path}"
    fi

    cleanup_merge
    trap - EXIT HUP INT TERM
}

if [ ! -f "${EXAMPLE_PATH}" ] && [ -f "${BUNDLED_EXAMPLE_PATH}" ]; then
    copy_or_die "${BUNDLED_EXAMPLE_PATH}" "${EXAMPLE_PATH}"
fi

if [ -d "${CONFIG_PATH}" ]; then
    if [ ! -s "${CONFIG_PATH}/config.yaml" ] && [ -f "${EXAMPLE_PATH}" ]; then
        copy_or_die "${EXAMPLE_PATH}" "${CONFIG_PATH}/config.yaml"
    fi
    CONFIG_PATH="${CONFIG_PATH}/config.yaml"
elif [ ! -s "${CONFIG_PATH}" ] && [ -f "${EXAMPLE_PATH}" ]; then
    copy_or_die "${EXAMPLE_PATH}" "${CONFIG_PATH}"
fi

merge_config_from_example "${CONFIG_PATH}"

exec python3 -m repeater.main --config "${CONFIG_PATH}"
