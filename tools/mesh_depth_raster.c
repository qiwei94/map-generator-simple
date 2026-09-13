#include <stdint.h>
#include <math.h>
#include <stddef.h>

/* Orthographic per-pixel triangle depth test; never edits input geometry. */
void raster(const double *tri, const uint8_t *gray, size_t n,
            int w, int h, double *depth, uint8_t *rgb) {
    for (size_t k=0; k<n; ++k) {
        const double *a=tri+9*k, *b=a+3, *c=a+6;
        const double den=(b[1]-c[1])*(a[0]-c[0])+(c[0]-b[0])*(a[1]-c[1]);
        if (fabs(den)<1e-12) continue;
        int xmin=(int)floor(fmin(a[0],fmin(b[0],c[0])));
        int xmax=(int)ceil(fmax(a[0],fmax(b[0],c[0])));
        int ymin=(int)floor(fmin(a[1],fmin(b[1],c[1])));
        int ymax=(int)ceil(fmax(a[1],fmax(b[1],c[1])));
        if (xmin<0) xmin=0; if (ymin<0) ymin=0;
        if (xmax>=w) xmax=w-1; if (ymax>=h) ymax=h-1;
        for (int y=ymin; y<=ymax; ++y) for (int x=xmin; x<=xmax; ++x) {
            double u=((b[1]-c[1])*(x+0.5-c[0])+(c[0]-b[0])*(y+0.5-c[1]))/den;
            double v=((c[1]-a[1])*(x+0.5-c[0])+(a[0]-c[0])*(y+0.5-c[1]))/den;
            double q=1-u-v;
            if (u < -1e-9 || v < -1e-9 || q < -1e-9) continue;
            double z=u*a[2]+v*b[2]+q*c[2];
            size_t i=(size_t)y*w+x;
            if (z>depth[i]) {
                depth[i]=z;
                rgb[3*i]=gray[k];rgb[3*i+1]=gray[k];rgb[3*i+2]=gray[k];
            }
        }
    }
}
