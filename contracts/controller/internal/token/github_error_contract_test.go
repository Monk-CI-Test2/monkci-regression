package token

import (
	"context"
	"fmt"
	"github.com/stretchr/testify/require"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"
)

func TestRegressionGitHubErrorsDoNotInventStateOrRetryInsideLookup(t *testing.T) {
	for _, code := range []int{403, 404, 429, 500, 502, 503} {
		t.Run(fmt.Sprint(code), func(t *testing.T) {
			calls := 0
			svc := &Service{httpClient: &http.Client{Transport: roundTripFunc(func(req *http.Request) (*http.Response, error) {
				calls++
				require.Equal(t, "/repos/test/project/actions/jobs/123", req.URL.Path)
				return &http.Response{StatusCode: code, Status: fmt.Sprint(code), Body: io.NopCloser(strings.NewReader(`{"message":"temporary error"}`)), Header: http.Header{"Retry-After": []string{"60"}}}, nil
			})}, installationTokenCache: map[int64]*InstallationToken{42: {Token: "mock-token", ExpiresAt: time.Now().Add(time.Hour)}}}
			state, err := svc.GetWorkflowJobStatus(context.Background(), 42, "test/project", 123)
			require.Error(t, err)
			require.Nil(t, state)
			require.Equal(t, 1, calls, "one lookup must not become an API retry storm")
		})
	}
}
